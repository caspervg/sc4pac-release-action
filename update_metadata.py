#!/usr/bin/env python3
"""Points the sc4pac metadata of one asset at a new release.

Updates the asset's version, lastModified and url, the version of every package that installs
files from it, and the sha256 of every file those packages install through `withChecksum`.
Values are replaced in the source text, so the diff touches nothing but the values that changed
and hand-written formatting survives.

    python update_metadata.py --channel-dir <checkout> --asset-id <id> --version <v> --url <url>
                              --last-modified <iso> --archive <file> [--src src/yaml] [--stex]
                              [--report report.json] [--summary summary.md] [--release-url <url>]
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone

import yaml

# A Simtropolis file page, as the channels' url-check finds it in nonPersistentUrl or url.
STEX_FILE_PAGE = re.compile(r"https?://community\.simtropolis\.com/files/file/\d+-[^/?#]+", re.IGNORECASE)


class File:
    def __init__(self, path, text, docs):
        self.path = path
        self.text = text
        self.docs = docs
        self.edits = []  # (start, end, replacement) in the source text


class Updater:
    def __init__(self, args):
        self.args = args
        self.asset_id = args.asset_id
        self.new_version = re.sub(r"^v", "", args.version, flags=re.IGNORECASE)
        with open(args.archive, "rb") as f:
            self.archive_sha256 = hashlib.sha256(f.read()).hexdigest()
        self.archive = read_zip(args.archive)
        self.report = {
            "assetId": self.asset_id,
            "packages": [],
            "checksums": [],
            "renamed": [],
            "warnings": [],
            "errors": [],
        }

    def run(self):
        files = list(self.files_mentioning_asset())
        definitions = [(f, e) for f in files for e in entries(f)
                       if scalar(e, "assetId") == self.asset_id and get(e, "url") is not None]
        if len(definitions) != 1:
            fail(f'expected one definition of asset "{self.asset_id}" under {self.src_dir()}, found {len(definitions)}')
        asset_file, asset = definitions[0]
        old_version = scalar(asset, "version")
        self.old_base = base_version(old_version)
        self.new_base = base_version(self.new_version)
        report = self.report
        last_modified = self.stex_last_modified(asset) if self.args.stex else self.args.last_modified
        report.update(
            file=os.path.relpath(asset_file.path, self.args.channel_dir).replace(os.sep, "/"),
            version=[old_version, None],
            url=[scalar(asset, "url"), self.args.url],
            lastModified=[scalar(asset, "lastModified"), last_modified],
        )

        # A job for an older release that finishes after the job for a newer one must not roll
        # the channel back.
        if compare_versions(self.new_base, self.old_base) < 0:
            report["version"][1] = old_version
            report["lastModified"][1] = report["lastModified"][0]
            report["warnings"].append(f"the channel already has {old_version}, which is newer than {self.new_version}")
            return self.finish([])

        # Packages that install files from the asset. The checksums come first, because when the
        # version does not change, only a changed file justifies a new release suffix.
        packages = []
        covered = set()
        files_changed = False
        for f in files:
            for pkg in entries(f):
                if get(pkg, "group") is None:
                    continue
                refs = [r for r in asset_refs(pkg) if scalar(r, "assetId") == self.asset_id]
                if not refs:
                    continue
                package_id = f'{scalar(pkg, "group")}:{scalar(pkg, "name")}'
                packages.append((f, pkg, package_id))
                for ref in refs:
                    for holder in [ref, *items(get(ref, "withConditions"))]:
                        for key in ("include", "exclude"):
                            for node in items(get(holder, key)):
                                pattern = self.rename_pattern(f, package_id, node, key)
                                if key == "include" and not self.match(pattern):
                                    report["warnings"].append(f"{package_id}: include `{pattern}` matches no file in the new archive")
                        for entry in items(get(holder, "withChecksum")):
                            pattern = self.rename_pattern(f, package_id, get(entry, "include"), "withChecksum")
                            matched = self.match(pattern)
                            if len(matched) != 1:
                                report["errors"].append(f"{package_id}: withChecksum include `{pattern}` matches {len(matched)} files in the new archive, expected 1")
                                continue
                            path = matched[0]
                            covered.add(path)
                            node = get(entry, "sha256")
                            old = node.value
                            new = self.archive[path] if old == old.lower() else self.archive[path].upper()
                            changed = old.lower() != new.lower()
                            if changed:
                                set_scalar(f, node, new)
                                files_changed = True
                            report["checksums"].append({"package": package_id, "file": path, "sha256": new, "changed": changed})
        if not packages:
            report["errors"].append(f'no package installs files from asset "{self.asset_id}"')

        # Files that sc4pac only installs with a checksum. A new DLL or INI in the release that no
        # package lists would be silently left out of the install.
        for path in self.archive:
            if re.search(r"\.(dll|asi|exe|ini)$", path, re.IGNORECASE) and path not in covered:
                report["warnings"].append(f"`{path}` is in the archive, but no package installs it (it needs a `withChecksum` entry)")

        final_version = self.new_version
        if self.new_base == self.old_base:
            # Same release again: keep the version unless the files changed underneath it, in
            # which case sc4pac needs a release suffix to reinstall them.
            final_version = next_release_suffix(old_version) if files_changed else old_version
        report["version"][1] = final_version

        # When the metadata already describes this release, lastModified stays too. It may hold the
        # time of the Simtropolis upload, which the main channel's update check compares against.
        # A time read from Simtropolis itself is that upload, so it still corrects a release that
        # went in with the GitHub time.
        if final_version == old_version and scalar(asset, "url") == self.args.url:
            if self.args.stex:
                set_scalar(asset_file, get(asset, "lastModified"), last_modified)
            else:
                report["lastModified"][1] = report["lastModified"][0]
            return self.finish([f for f in files if f.edits])

        set_scalar(asset_file, get(asset, "version"), final_version)
        set_scalar(asset_file, get(asset, "lastModified"), last_modified)
        set_scalar(asset_file, get(asset, "url"), self.args.url)
        checksum = get(get(asset, "checksum"), "sha256")
        if checksum is not None:
            set_scalar(asset_file, checksum, self.archive_sha256 if checksum.value == checksum.value.lower() else self.archive_sha256.upper())

        for f, pkg, package_id in packages:
            node = get(pkg, "version")
            old = node.value
            if base_version(old) != self.old_base:
                report["warnings"].append(f"{package_id} is at version {old} while asset {self.asset_id} was at {old_version}, so its version was left alone")
                continue
            set_scalar(f, node, final_version)
            report["packages"].append({"id": package_id, "version": [old, final_version]})

            # A warning shown at install time often names a versioned installer or the
            # `group.name.version.sc4pac` folder it lands in.
            warning = get(get(pkg, "info"), "warning")
            if isinstance(warning, yaml.ScalarNode) and final_version != old:
                start, end = warning.start_mark.index, warning.end_mark.index
                raw = f.text[start:end]
                renamed = replace_version(raw, base_version(old), base_version(final_version))
                if renamed != raw:
                    f.edits.append((start, end, renamed))
                    report["renamed"].append({"package": package_id, "field": "info.warning", "from": base_version(old), "to": base_version(final_version)})

        return self.finish([f for f in files if f.edits])

    # lastModified as Simtropolis states it. The channels' url-check compares lastModified with the
    # STEX upload, which comes after the GitHub release, so the time is only taken once that upload
    # is there: no older than the release archive, and with the release's version.
    def stex_last_modified(self, asset):
        source = scalar(asset, "nonPersistentUrl") or scalar(asset, "url") or ""
        m = STEX_FILE_PAGE.match(source)
        if not m:
            fail(f'asset "{self.asset_id}" has no Simtropolis nonPersistentUrl or url, so no STEX upload time applies to it')
        page = m.group(0) + "/"
        details = read_stex_page(page, self.args.stex_page)
        uploaded = parse_time(details["dateModified"], page)
        released = parse_time(self.args.last_modified, "--last-modified")
        shown = str(details.get("softwareVersion") or "").strip()
        if uploaded < released:
            fail(f"{page} was last updated at {iso_time(uploaded)}, before the release archive ({iso_time(released)}). "
                 "Upload the release to the STEX first.")
        if base_version(re.sub(r"^v", "", shown, flags=re.IGNORECASE)).lower() != base_version(self.new_version).lower():
            fail(f'{page} shows version "{shown}", not {self.new_version}. Upload the release to the STEX first, or correct its version there.')
        self.report["stex"] = {"page": page, "version": shown, "updated": iso_time(uploaded)}
        return iso_time(uploaded)

    # An include pattern that names a versioned file (`/Setup-0.8.2.exe`) follows the version, but
    # only when the pattern as written no longer matches.
    def rename_pattern(self, f, package_id, node, field):
        if not isinstance(node, yaml.ScalarNode):
            fail(f"{package_id}: {field} entry without an include pattern")
        pattern = node.value
        if self.old_base == self.new_base or self.match(pattern):
            return pattern
        renamed = replace_version(pattern, self.old_base, self.new_base)
        if renamed == pattern or not self.match(renamed):
            return pattern
        set_scalar(f, node, renamed)
        self.report["renamed"].append({"package": package_id, "field": field, "from": pattern, "to": renamed})
        return renamed

    # sc4pac matches include and exclude patterns as case-insensitive regular expressions against
    # a file's path inside the archive, which starts with a slash.
    def match(self, pattern):
        regex = re.compile(pattern, re.IGNORECASE)
        return [path for path in self.archive if regex.search(path)]

    def files_mentioning_asset(self):
        for root, _, names in os.walk(self.src_dir()):
            for name in sorted(names):
                if not name.endswith(".yaml"):
                    continue
                path = os.path.join(root, name)
                # newline="" keeps the file's line endings, so offsets and the written file match.
                with open(path, encoding="utf-8", newline="") as fp:
                    text = fp.read()
                if self.asset_id not in text:
                    continue
                try:
                    docs = list(yaml.compose_all(text, Loader=yaml.SafeLoader))
                except yaml.YAMLError as e:
                    fail(f"{path}: {e}")
                yield File(path, text, docs)

    def src_dir(self):
        return os.path.join(self.args.channel_dir, self.args.src)

    # Writes the edited files unless something failed, then the report and summary.
    def finish(self, changed):
        report = self.report
        if not report["errors"]:
            for f in changed:
                with open(f.path, "w", encoding="utf-8", newline="") as fp:
                    fp.write(apply_edits(f.text, f.edits))
        report["changed"] = not report["errors"] and bool(changed)
        if self.args.report:
            with open(self.args.report, "w", encoding="utf-8", newline="\n") as fp:
                json.dump(report, fp, indent=2)
                fp.write("\n")
        if self.args.summary:
            with open(self.args.summary, "w", encoding="utf-8", newline="\n") as fp:
                fp.write(summarize(report, self.args.release_url))
        for warning in report["warnings"]:
            print(f"warning: {warning}")
        for error in report["errors"]:
            print(f"error: {error}", file=sys.stderr)
        if report["changed"]:
            names = ", ".join(os.path.basename(f.path) for f in changed)
            print(f'{self.asset_id}: {report["version"][0]} -> {report["version"][1]} in {names}')
        else:
            print(f"{self.asset_id}: nothing to change")
        return 1 if report["errors"] else 0


# Assets and packages, both for one definition per document and for the `packages`/`assets`
# arrays that a document may hold instead.
def entries(f):
    for doc in f.docs:
        if not isinstance(doc, yaml.MappingNode):
            continue
        if get(doc, "group") is not None or get(doc, "assetId") is not None:
            yield doc
        else:
            for key in ("packages", "assets"):
                yield from (item for item in items(get(doc, key)) if isinstance(item, yaml.MappingNode))


def asset_refs(pkg):
    refs = items(get(pkg, "assets"))
    for variant in items(get(pkg, "variants")):
        refs += items(get(variant, "assets"))
    return [ref for ref in refs if isinstance(ref, yaml.MappingNode)]


def get(node, key):
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode) and k.value == key:
                return v
    return None


def scalar(node, key):
    value = get(node, key)
    return value.value if isinstance(value, yaml.ScalarNode) else None


def items(node):
    return list(node.value) if isinstance(node, yaml.SequenceNode) else []


def set_scalar(f, node, value):
    if not isinstance(node, yaml.ScalarNode):
        fail(f"{f.path}: expected a scalar for {value}")
    if node.value != value:
        f.edits.append((node.start_mark.index, node.end_mark.index, render_scalar(node, value)))


# Keeps the quoting the value had. A plain scalar stays plain only while it still reads back as
# the same string, so a version such as 1.0 cannot turn into a number.
def render_scalar(node, value):
    if node.style == "'":
        return "'" + value.replace("'", "''") + "'"
    if node.style is None and " #" not in value and yaml.safe_load(f"k: {value}") == {"k": value}:
        return value
    return json.dumps(value, ensure_ascii=False)


def apply_edits(text, edits):
    edits = sorted(edits, reverse=True)
    for (start, _, _), (_, end, _) in zip(edits, edits[1:]):
        if end > start:
            fail("overlapping edits")
    for start, end, value in edits:
        text = text[:start] + value + text[end:]
    return text


# Replaces a whole version number, so 0.8.2 is not found inside 10.8.2 or 0.8.25. Escaped dots
# (0\.8\.2) are handled for regex patterns.
def replace_version(text, old, new):
    for a, b in ((old, new), (old.replace(".", "\\."), new.replace(".", "\\."))):
        regex = re.compile(r"(?<!\d)(?<!\d\.)" + re.escape(a) + r"(?!\d)(?!\.\d)")
        text = regex.sub(lambda _: b, text)
    return text


def base_version(version):
    return re.sub(r"-\d+$", "", version)


def next_release_suffix(version):
    m = re.fullmatch(r"(.*)-(\d+)", version)
    return f"{m.group(1)}-{int(m.group(2)) + 1}" if m else f"{version}-1"


# Compares dotted versions part by part, numerically where both parts are numbers.
def compare_versions(a, b):
    pa, pb = re.split(r"[.-]", a), re.split(r"[.-]", b)
    for i in range(max(len(pa), len(pb))):
        x = pa[i] if i < len(pa) else "0"
        y = pb[i] if i < len(pb) else "0"
        d = int(x) - int(y) if x.isdigit() and y.isdigit() else (x > y) - (x < y)
        if d:
            return 1 if d > 0 else -1
    return 0


# The details of a file from the schema.org JSON-LD on its Simtropolis page. Its dateModified is
# the upload time that the STEX API reports, without the API's key from the Simtropolis admins.
# The block is matched on the file ID, because Simtropolis redirects an outdated name in the URL.
def read_stex_page(url, saved=None):
    if saved:
        with open(saved, encoding="utf-8") as fp:
            html = fp.read()
    else:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Firefox/130.0"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                html = response.read().decode("utf-8", "replace")
        except OSError as e:
            fail(f"cannot read {url}: {e}")
    file_id = stex_file_id(url)
    for m in re.finditer(r"<script type=['\"]application/ld\+json['\"]>(.*?)</script>", html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("dateModified") and stex_file_id(str(data.get("url", ""))) == file_id:
            return data
    fail(f"{url} states no upload time (schema.org dateModified)")


def stex_file_id(url):
    m = re.search(r"/files/file/(\d+)-", url)
    return m.group(1) if m else None


# Simtropolis writes 2026-11-10T20:13:52+0000, GitHub 2026-11-10T20:13:52Z.
def parse_time(text, source):
    try:
        return datetime.strptime(text.strip(), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        fail(f"{source}: unexpected time {text!r}")


# The form the channels write lastModified in.
def iso_time(t):
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# The sha256 of every file in a ZIP archive, keyed by its sc4pac path ('/dir/file.dll').
def read_zip(path):
    try:
        with zipfile.ZipFile(path) as zf:
            return {"/" + info.filename.lstrip("/"): hashlib.sha256(zf.read(info)).hexdigest()
                    for info in zf.infolist() if not info.is_dir()}
    except zipfile.BadZipFile:
        fail(f"{path} is not a ZIP archive")


def summarize(r, release_url):
    lines = []
    pkgs = ", ".join(f'`{p["id"]}`' for p in r["packages"]) or f'`{r["assetId"]}`'
    source = f" from [this release]({release_url})" if release_url else ""
    lines += [f'Updates {pkgs} to **{r["version"][1]}**{source}.', ""]
    lines += ["| | Before | After |", "|---|---|---|"]
    for key in ("version", "lastModified", "url"):
        lines.append(f"| {key} | {r[key][0]} | {r[key][1]} |")
    lines.append("")
    if r.get("stex"):
        s = r["stex"]
        lines += [f'lastModified is the time of the upload of {s["version"]} to [Simtropolis]({s["page"]}).', ""]
    if r["checksums"]:
        lines += ["Checksums of the files sc4pac installs, computed from the release archive:", ""]
        lines += ["| File | sha256 | |", "|---|---|---|"]
        for c in r["checksums"]:
            lines.append(f'| `{c["file"]}` | `{c["sha256"]}` | {"changed" if c["changed"] else "unchanged"} |')
        lines.append("")
    lines += [f'- {x["package"]}: {x["field"]} `{x["from"]}` → `{x["to"]}`' for x in r["renamed"]]
    lines += [f"- ⚠️ {w}" for w in r["warnings"]]
    lines += [f"- ❌ {e}" for e in r["errors"]]
    return "\n".join(lines) + "\n"


# In a workflow the message also becomes an annotation, so it shows on the run's page.
def fail(message):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=sc4pac::{escaped}")
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--channel-dir", default=".")
    parser.add_argument("--src", default="src/yaml")
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--version", required=True, help="release version or tag, e.g. v1.2.0")
    parser.add_argument("--url", required=True, help="download URL of the release archive")
    parser.add_argument("--last-modified", required=True, help="upload time of the archive, e.g. 2026-10-30T13:38:52Z")
    parser.add_argument("--archive", required=True, help="local copy of the file at --url")
    parser.add_argument("--stex", action="store_true",
                        help="take lastModified from the asset's Simtropolis page instead; --last-modified must then be the release time, which the upload may not be older than")
    parser.add_argument("--stex-page", help="read this saved copy of the Simtropolis page instead of downloading it")
    parser.add_argument("--report", help="write a JSON report here")
    parser.add_argument("--summary", help="write a Markdown summary here")
    parser.add_argument("--release-url", help="release page to link from the summary")
    return Updater(parser.parse_args()).run()


if __name__ == "__main__":
    sys.exit(main())

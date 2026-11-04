"""Build, verify and publish the fixed contrib distributions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tarfile
import tomllib
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path
from tempfile import TemporaryDirectory


def project() -> dict:
    return tomllib.loads(Path("pyproject.toml").read_text())["project"]


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def prepare(version: str) -> None:
    if (
        re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(rc[1-9]\d*)?", version)
        is None
    ):
        raise ValueError("expected a version such as 0.3.0rc1 or 0.3.0")
    subprocess.run(["uv", "version", version, "--frozen"], check=True)
    subprocess.run(["uv", "lock", "--prerelease", "allow"], check=True)


def build(directory: Path) -> None:
    metadata = project()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    epoch = subprocess.check_output(
        ["git", "show", "-s", "--format=%ct", "HEAD"], text=True
    ).strip()
    subprocess.run(
        ["uv", "build", "--no-sources", "--out-dir", str(directory)],
        env={**os.environ, "SOURCE_DATE_EPOCH": epoch},
        check=True,
    )
    files = []
    for path in sorted(directory.glob("*")):
        if path.suffix == ".whl":
            kind = "wheel"
            with zipfile.ZipFile(path) as archive:
                info = BytesParser().parsebytes(
                    archive.read(
                        next(
                            n
                            for n in archive.namelist()
                            if n.endswith(".dist-info/METADATA")
                        )
                    )
                )
        elif path.name.endswith(".tar.gz"):
            kind = "sdist"
            with tarfile.open(path) as archive:
                member = next(
                    m for m in archive.getmembers() if m.name.endswith("/PKG-INFO")
                )
                handle = archive.extractfile(member)
                assert handle is not None
                with handle:
                    info = BytesParser().parsebytes(handle.read())
        else:
            continue
        if (info["Name"], info["Version"], info["License-Expression"]) != (
            metadata["name"],
            metadata["version"],
            metadata["license"],
        ):
            raise ValueError(
                f"{path.name}: package metadata differs from pyproject.toml"
            )
        files.append({"filename": path.name, "kind": kind, "sha256": digest(path)})
    if sorted(file["kind"] for file in files) != ["sdist", "wheel"]:
        raise ValueError("release requires one wheel and one sdist")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "name": metadata["name"],
                "version": metadata["version"],
                "release_commit": commit,
                "source_date_epoch": int(epoch),
                "files": files,
            },
            indent=2,
        )
        + "\n"
    )


def inspect(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    metadata = project()
    if (manifest["release_commit"], manifest["name"], manifest["version"]) != (
        commit,
        metadata["name"],
        metadata["version"],
    ):
        raise ValueError("distributions do not belong to this release checkout")
    for file in manifest["files"]:
        path = directory / file["filename"]
        if (
            path.parent.resolve() != directory.resolve()
            or digest(path) != file["sha256"]
        ):
            raise ValueError(f"{file['filename']}: release checksum differs")
    return manifest


def verify(directory: Path, pypi: bool) -> None:
    manifest = inspect(directory)
    if pypi:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{manifest['name']}/{manifest['version']}/json"
        ) as response:
            published = json.load(response)
        hashes = {
            file["filename"]: file["digests"]["sha256"] for file in published["urls"]
        }
        for file in manifest["files"]:
            if hashes.get(file["filename"]) != file["sha256"]:
                raise ValueError(
                    f"PyPI {file['filename']}: missing or different artifact"
                )
    for kind in ["pypi"] if pypi else ["wheel", "sdist"]:
        with TemporaryDirectory(prefix="contrib-release-") as temporary:
            environment = Path(temporary) / "environment"
            subprocess.run(
                ["uv", "venv", "--python", "3.15", str(environment)], check=True
            )
            python = environment / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            requirements = (
                [f"{manifest['name']}=={manifest['version']}"]
                if pypi
                else [
                    str((directory / file["filename"]).resolve())
                    for file in manifest["files"]
                    if file["kind"] == kind
                ]
            )
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    str(python),
                    "--default-index",
                    "https://pypi.org/simple",
                    "--prerelease",
                    "allow",
                    *(["--refresh"] if pypi else []),
                    *requirements,
                ],
                cwd=temporary,
                check=True,
            )
            subprocess.run(
                [
                    str(python),
                    "-I",
                    "-c",
                    (
                        "import importlib.metadata as m; "
                        "from bazaar_core import Contrib; "
                        f"d=m.distribution({manifest['name']!r}); "
                        f"assert d.version == {manifest['version']!r}; "
                        "entries=[e for e in d.entry_points "
                        "if e.group == 'bazaar_compute_node.contribs']; "
                        "assert entries and all(isinstance(e.load(), Contrib) for e in entries); "
                        "print(d.metadata['Name'], d.version, [e.name for e in entries])"
                    ),
                ],
                cwd=temporary,
                check=True,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare").add_argument("version")
    for name in ("build", "inspect", "verify", "publish"):
        command = commands.add_parser(name)
        command.add_argument("--directory", type=Path, default=Path("dist"))
        if name == "verify":
            command.add_argument("--pypi", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.version)
    elif args.command == "build":
        build(args.directory)
    elif args.command == "inspect":
        print(json.dumps(inspect(args.directory), indent=2))
    elif args.command == "verify":
        verify(args.directory, args.pypi)
    else:
        manifest = inspect(args.directory)
        subprocess.run(
            [
                "uv",
                "publish",
                "--trusted-publishing",
                "always",
                *[str(args.directory / file["filename"]) for file in manifest["files"]],
            ],
            check=True,
        )


if __name__ == "__main__":
    main()

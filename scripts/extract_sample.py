"""Extract a small sample of <Job> records from a full Jobg8 feed ZIP.

Streams the XML member directly out of the ZIP and stops after the first
N <Job> elements, so this never reads the full feed (which the spec puts
at roughly 170 MB zipped / 1 GB uncompressed) into memory or off disk.
"""

import argparse
import zipfile
from pathlib import Path

from lxml import etree

DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "sample_feed.xml"
JOB_TAG = "Job"


def find_xml_member(zf: zipfile.ZipFile) -> str:
    """Return the single .xml member name inside the ZIP, or raise."""
    xml_members = [name for name in zf.namelist() if name.lower().endswith(".xml")]
    if not xml_members:
        raise ValueError("No .xml member found in the ZIP archive")
    if len(xml_members) > 1:
        raise ValueError(f"Expected exactly one .xml member, found: {xml_members}")
    return xml_members[0]


def extract_sample(zip_path: Path, output_path: Path, max_records: int) -> int:
    """Stream the first max_records <Job> elements into a new XML file.

    Returns the number of records written. Uses the same iterparse
    configuration as app.imports.parser.parse_job_feed, since the input
    is untrusted feed content here too.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        member_name = find_xml_member(zf)

        with zf.open(member_name) as stream:
            context = etree.iterparse(
                stream,
                events=("end",),
                load_dtd=False,
                no_network=True,
                resolve_entities=False,
                recover=False,
                huge_tree=True,
            )

            new_root: etree._Element | None = None
            count = 0

            for _, element in context:
                if not isinstance(element.tag, str) or etree.QName(element).localname != JOB_TAG:
                    continue

                if new_root is None:
                    parent = element.getparent()
                    root_tag = parent.tag if parent is not None else "Jobs"
                    new_root = etree.Element(root_tag)

                # Appending an already-parsed element onto a different tree
                # reparents it (lxml detaches it from the source tree), so
                # no separate copy step is needed to preserve its content.
                new_root.append(element)
                count += 1

                if count >= max_records:
                    break

    if new_root is None:
        raise ValueError(f"No <{JOB_TAG}> elements found in {member_name}")

    etree.ElementTree(new_root).write(
        str(output_path),
        xml_declaration=True,
        encoding="UTF-8",
        pretty_print=True,
    )
    return count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract a small sample of <Job> records from a Jobg8 feed ZIP."
    )
    parser.add_argument("zip_path", type=Path, help="Path to the input feed ZIP file")
    parser.add_argument(
        "output_path",
        type=Path,
        nargs="?",
        default=DEFAULT_OUTPUT,
        help=f"Path to write the sample XML file (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--max-records",
        type=int,
        default=50,
        help="Number of <Job> records to extract (default: 50)",
    )
    args = parser.parse_args()

    count = extract_sample(args.zip_path, args.output_path, args.max_records)
    size_bytes = args.output_path.stat().st_size
    print(f"Extracted {count} records to {args.output_path} ({size_bytes:,} bytes)")


if __name__ == "__main__":
    main()

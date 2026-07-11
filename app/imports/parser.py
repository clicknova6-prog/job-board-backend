"""Streaming parser for the Jobg8 XML feed."""

from os import PathLike
from typing import BinaryIO, Iterator, TypeAlias

from lxml import etree

from app.imports.schemas import JobFeedRecord


XMLSource: TypeAlias = str | PathLike[str] | BinaryIO


def job_element_to_dict(element: etree._Element) -> dict[str, str | None]:
    """Convert one flat ``<Job>`` element to a dictionary keyed by XML tag name.

    Jobg8's documented ``<Job>`` structure has one direct child element per
    field. ``QName.localname`` keeps this parser compatible with a namespaced
    feed without changing the field names expected by ``JobFeedRecord``.
    """
    return {
        etree.QName(child).localname: child.text
        for child in element
        if isinstance(child.tag, str)
    }


def parse_job_feed(source: XMLSource) -> Iterator[JobFeedRecord]:
    """Yield validated jobs from an XML feed without loading the feed into memory.

    Validation and XML syntax errors intentionally propagate to the caller.
    The import layer can then fail the import run with the exact bad record or
    XML error instead of silently discarding data.
    """
    context = etree.iterparse(
        source,
        events=("end",),
        # Feed files are untrusted input. Disable DTD loading, external entity
        # resolution, and network access to prevent XXE-style XML attacks.
        load_dtd=False,
        no_network=True,
        resolve_entities=False,
        recover=False,
        huge_tree=True,
    )

    for _, element in context:
        # Do not depend on an XML namespace being absent. The feed specification
        # names the element Job, and localname preserves that comparison.
        if not isinstance(element.tag, str) or etree.QName(element).localname != "Job":
            continue

        try:
            record = JobFeedRecord.model_validate(job_element_to_dict(element))
            # Suspending at yield keeps exactly one validated record available
            # to the caller at a time.
            yield record
        finally:
            # Clearing the completed Job releases its child text and elements.
            # Removing preceding siblings releases earlier Job elements that
            # lxml would otherwise keep attached to the root while streaming.
            element.clear()
            parent = element.getparent()
            if parent is not None:
                while element.getprevious() is not None:
                    del parent[0]

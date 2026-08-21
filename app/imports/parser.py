"""Streaming parser for the Jobg8 XML feed."""

from collections.abc import Callable
from os import PathLike
from typing import BinaryIO, Iterator, TypeAlias

from lxml import etree
from pydantic import ValidationError

from app.imports.schemas import JobFeedRecord


XMLSource: TypeAlias = str | PathLike[str] | BinaryIO
ValidationErrorHandler: TypeAlias = Callable[[dict[str, str | None], ValidationError], None]


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


def parse_job_feed(
    source: XMLSource,
    *,
    on_validation_error: ValidationErrorHandler | None = None,
) -> Iterator[JobFeedRecord]:
    """Yield validated jobs from an XML feed without loading the feed into memory.

    XML syntax errors always propagate to the caller. By default, Pydantic
    validation errors also propagate. A caller may provide
    ``on_validation_error`` to record invalid jobs and continue streaming the
    rest of the snapshot.
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
            raw_record = job_element_to_dict(element)
            try:
                record = JobFeedRecord.model_validate(raw_record)
            except ValidationError as error:
                # Validation errors are only handled when the caller explicitly
                # opts in. This keeps fail-fast behaviour available to other users
                # while allowing ImportService to count bad feed records.
                if on_validation_error is None:
                    raise
                on_validation_error(raw_record, error)
                continue

            # Suspending at yield keeps exactly one validated record available
            # to the caller at a time. The finally block cleans it up only when
            # iteration resumes, as required for streaming memory usage.
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

"""Unit tests for the streaming Jobg8 XML parser."""

from __future__ import annotations

import inspect
from io import BytesIO

import pytest
from lxml import etree
from pydantic import ValidationError

from app.imports.parser import job_element_to_dict, parse_job_feed


def _job_xml(
    sender_reference: str,
    *,
    title: str = "Software Engineer",
    description: str = "Build and maintain software.",
    application_url: str = "https://example.com/jobs/123",
    optional_elements: str = "",
) -> str:
    return f"""
    <Job>
      <SenderReference>{sender_reference}</SenderReference>
      <Position>{title}</Position>
      <Description>{description}</Description>
      <ApplicationURL>{application_url}</ApplicationURL>
      {optional_elements}
    </Job>
    """


def test_job_element_to_dict_uses_local_names_and_ignores_comments() -> None:
    element = etree.fromstring(
        b"""
        <feed:Job xmlns:feed="urn:jobg8">
          <feed:SenderReference>job-1</feed:SenderReference>
          <!-- comments are not feed fields -->
          <feed:Location />
        </feed:Job>
        """
    )

    assert job_element_to_dict(element) == {
        "SenderReference": "job-1",
        "Location": None,
    }


def test_well_formed_feed_streams_multiple_valid_jobs() -> None:
    xml = f"""
    <Jobs>
      <Metadata>ignored</Metadata>
      {_job_xml("job-1", optional_elements="<EmploymentType>FT</EmploymentType>")}
      {_job_xml("job-2", title="Nurse", application_url="https://example.com/jobs/2")}
    </Jobs>
    """.encode()

    records = parse_job_feed(BytesIO(xml))

    assert inspect.isgenerator(records)
    parsed = list(records)
    assert [record.sender_reference for record in parsed] == ["job-1", "job-2"]
    assert [record.title for record in parsed] == ["Software Engineer", "Nurse"]
    assert parsed[0].employment_type == "full_time"
    assert parsed[0].source_record["EmploymentType"] == "FT"
    assert parsed[1].apply_url == "https://example.com/jobs/2"


@pytest.mark.parametrize(
    "xml",
    [
        b"<Jobs />",
        b"<Jobs><Metadata>snapshot</Metadata><!-- no jobs --></Jobs>",
    ],
)
def test_feed_without_job_elements_yields_nothing(xml: bytes) -> None:
    assert list(parse_job_feed(BytesIO(xml))) == []


def test_root_job_element_parses_without_a_parent() -> None:
    xml = _job_xml("root-job").encode()

    records = list(parse_job_feed(BytesIO(xml)))

    assert len(records) == 1
    assert records[0].sender_reference == "root-job"


def test_missing_and_empty_optional_elements_become_none() -> None:
    xml = f"""
    <Jobs>
      {_job_xml(
          "optional-job",
          optional_elements='''
            <Location />
            <SalaryMinimum></SalaryMinimum>
            <LogoURL />
          ''',
      )}
    </Jobs>
    """.encode()

    record = next(parse_job_feed(BytesIO(xml)))

    assert record.location is None
    assert record.salary_min is None
    assert record.advertiser_logo_url is None
    assert record.country_name is None
    assert record.fallback_fields == set()


def test_namespaced_feed_and_job_elements_parse_normally() -> None:
    xml = b"""
    <feed:Jobs xmlns:feed="urn:jobg8">
      <feed:Job>
        <feed:SenderReference>namespaced-job</feed:SenderReference>
        <feed:Position>Engineer</feed:Position>
        <feed:Description>Build systems.</feed:Description>
        <feed:ApplicationURL>https://example.com/jobs/ns</feed:ApplicationURL>
      </feed:Job>
    </feed:Jobs>
    """

    record = next(parse_job_feed(BytesIO(xml)))

    assert record.sender_reference == "namespaced-job"
    assert record.title == "Engineer"


def test_validation_error_raises_by_default() -> None:
    xml = f"""
    <Jobs>
      {_job_xml("invalid-job", title="")}
    </Jobs>
    """.encode()

    with pytest.raises(ValidationError) as exc_info:
        list(parse_job_feed(BytesIO(xml)))

    assert "Position" in {error["loc"][0] for error in exc_info.value.errors()}


def test_validation_callback_receives_bad_raw_record_and_stream_continues() -> None:
    invalid = _job_xml("invalid-job", application_url="")
    valid = _job_xml("valid-job", application_url="https://example.com/jobs/valid")
    xml = f"<Jobs>{invalid}{valid}</Jobs>".encode()
    validation_failures: list[tuple[dict[str, str | None], ValidationError]] = []

    records = list(
        parse_job_feed(
            BytesIO(xml),
            on_validation_error=lambda raw, error: validation_failures.append(
                (raw, error)
            ),
        )
    )

    assert [record.sender_reference for record in records] == ["valid-job"]
    assert len(validation_failures) == 1
    raw_record, error = validation_failures[0]
    assert raw_record["SenderReference"] == "invalid-job"
    assert raw_record["ApplicationURL"] is None
    assert "ApplicationURL" in {item["loc"][0] for item in error.errors()}


def test_truncated_feed_yields_completed_job_then_raises_xml_syntax_error() -> None:
    xml = (
        f"<Jobs>{_job_xml('complete-job')}"
        "<Job><SenderReference>truncated-job</SenderReference>"
    ).encode()
    records = parse_job_feed(BytesIO(xml))

    first = next(records)

    assert first.sender_reference == "complete-job"
    with pytest.raises(etree.XMLSyntaxError):
        next(records)


def test_malformed_xml_before_any_complete_job_raises() -> None:
    xml = b"<Jobs><Job><SenderReference>broken</SenderReference></Jobs>"

    with pytest.raises(etree.XMLSyntaxError):
        list(parse_job_feed(BytesIO(xml)))

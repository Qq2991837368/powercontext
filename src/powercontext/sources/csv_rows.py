# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Row-level capture of an offline CSV document as individual Sources."""

from __future__ import annotations

import csv
import io
import json
from urllib.parse import quote

from pydantic import BaseModel, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

CSV_ROW_SOURCE_NAME = "csv-row"
_UTF8_BOM = b"\xef\xbb\xbf"


class CSVDocumentInput(BaseModel):
    """One complete offline CSV payload plus the identity it is captured under."""

    document_id: str
    primary_key: str
    content: bytes


class CSVRowInput(BaseModel):
    """The adapter-native value of one captured row."""

    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


class CSVRowSource(Source):
    """One captured CSV record kept with its original columns and values."""

    source_id: str
    document_id: str
    primary_key: str
    primary_key_value: str
    columns: tuple[str, ...]
    values: tuple[str, ...]


def csv_row_source_id(document_id: str, primary_key_value: str) -> str:
    """Build the delimiter-safe, bounded identity of one captured CSV row."""

    _validate_document_id(document_id)
    if not isinstance(primary_key_value, str) or not primary_key_value.strip():
        raise ValueError("primary_key_value must be a non-empty, non-blank string")  # noqa: TRY003

    identity = f"csv:{quote(document_id, safe='')}:{quote(primary_key_value, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(  # noqa: TRY003
            f"csv row source id must not exceed {MAX_SOURCE_ID_LENGTH} characters"
        )
    return identity


def parse_csv_document(value: CSVDocumentInput) -> tuple[CSVRowInput, ...]:
    """Parse and fully validate one CSV document into row-level inputs."""

    _validate_document_id(value.document_id)
    if not value.primary_key:
        raise ValueError("primary_key must be a non-empty string")  # noqa: TRY003

    header, records = _read_records(_decode(value.content))
    _validate_header(header, value.primary_key)
    column_index = header.index(value.primary_key)

    rows: list[CSVRowInput] = []
    seen_keys: set[str] = set()
    for record in records:
        if not record:
            # A completely empty physical record carries no data and is ignored.
            continue
        if len(record) != len(header):
            raise ValueError(  # noqa: TRY003
                f"record has {len(record)} fields, expected {len(header)}"
            )
        primary_key_value = record[column_index]
        if not primary_key_value.strip():
            raise ValueError("primary key value must not be empty or blank")  # noqa: TRY003
        if primary_key_value in seen_keys:
            raise ValueError(  # noqa: TRY003
                f"duplicate primary key value {primary_key_value!r}"
            )
        seen_keys.add(primary_key_value)
        rows.append(
            CSVRowInput(
                document_id=value.document_id,
                primary_key=value.primary_key,
                primary_key_value=primary_key_value,
                columns=header,
                values=tuple(record),
            )
        )

    return tuple(rows)


def _validate_document_id(document_id: str) -> None:
    if not isinstance(document_id, str) or not document_id:
        raise ValueError("document_id must be a non-empty string")  # noqa: TRY003
    if document_id != document_id.strip():
        raise ValueError(  # noqa: TRY003
            "document_id must not contain leading or trailing whitespace"
        )


def _decode(content: bytes) -> str:
    if content.startswith(_UTF8_BOM):
        content = content[len(_UTF8_BOM) :]
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("CSV content must be valid UTF-8") from error  # noqa: TRY003


def _read_records(text: str) -> tuple[tuple[str, ...], list[list[str]]]:
    reader = csv.reader(
        io.StringIO(text, newline=""),
        delimiter=",",
        quotechar='"',
        doublequote=True,
        strict=True,
    )
    try:
        records = list(reader)
    except csv.Error as error:
        raise ValueError(f"malformed CSV document: {error}") from error  # noqa: TRY003
    if not records:
        raise ValueError("CSV document must contain a header record")  # noqa: TRY003
    return tuple(records[0]), records[1:]


def _validate_header(header: tuple[str, ...], primary_key: str) -> None:
    if not header:
        raise ValueError("CSV header must not be empty")  # noqa: TRY003
    for name in header:
        if not name.strip():
            raise ValueError("CSV header fields must not be empty or blank")  # noqa: TRY003
    if len(set(header)) != len(header):
        raise ValueError("CSV header fields must be unique")  # noqa: TRY003
    if primary_key not in header:
        raise ValueError(  # noqa: TRY003
            f"primary_key {primary_key!r} must match one CSV header field exactly"
        )


class CSVRowSourceAdapter:
    """Resolve and read one captured CSV row."""

    input_class = CSVRowInput
    name = CSV_ROW_SOURCE_NAME
    source_class = CSVRowSource

    async def resolve(self, value: CSVRowInput, /) -> CSVRowSource:
        source_id = csv_row_source_id(value.document_id, value.primary_key_value)
        return CSVRowSource(
            name=source_id,
            source_id=source_id,
            materialization=SourceMaterialization.CAPTURED,
            document_id=value.document_id,
            primary_key=value.primary_key,
            primary_key_value=value.primary_key_value,
            columns=value.columns,
            values=value.values,
        )

    async def read(self, source: CSVRowSource, /) -> CSVRowInput:
        return CSVRowInput(
            document_id=source.document_id,
            primary_key=source.primary_key,
            primary_key_value=source.primary_key_value,
            columns=source.columns,
            values=source.values,
        )


class CSVRowTextEvidenceProjection:
    """Expose one CSV row as canonical textual evidence without type coercion."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = CSVRowSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: CSVRowSource, /) -> TextEvidence:
        row: dict[str, JsonValue] = {
            column: value for column, value in zip(source.columns, source.values, strict=True)
        }
        return TextEvidence(
            source_type=CSV_ROW_SOURCE_NAME,
            source_id=source.source_id,
            content=json.dumps(row, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "document_id": source.document_id,
                "primary_key": source.primary_key,
                "primary_key_value": source.primary_key_value,
                "columns": list(source.columns),
                "row": row,
            },
        )


CSV_ROW_SOURCE_ADAPTER = CSVRowSourceAdapter()
CSV_ROW_SOURCE_DEFINITION = AdapterSourceDefinition(
    CSV_ROW_SOURCE_ADAPTER,
    projections=(CSVRowTextEvidenceProjection(),),
)

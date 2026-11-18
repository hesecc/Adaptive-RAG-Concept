"""Authenticated file management. Content lookup never accepts an object key from HTTP."""

import base64
import binascii
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from fastapi import Depends, Request
from fastapi.responses import Response
from pydantic import Field

from .http_inputs import AssetInput, AssetUpdate, DocumentInput, Input
from .config import Principal
from .domain import ConflictError, ForbiddenError, IngestRequest, NotFoundError, ValidationError
from .pdf import PdfSettings


class FileTrash(Input):
    expected_generation: str | None = None
    expected_revision: str | None = None


class FileRestore(Input):
    trash_id: str = Field(min_length=1, max_length=128)
    expected_generation: str | None = None


class PdfUpload(Input):
    document_id: str
    version: str
    title: str
    data_domains: list[str] = Field(default_factory=list, max_length=6)
    content_base64: str = Field(min_length=1, max_length=13_333_336)


def _decode(value):
    try:
        content = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise ValidationError("invalid file encoding") from None
    if not 1 <= len(content) <= 10_000_000:
        raise ValidationError("file size must be between 1 and 10000000 bytes")
    return content


def _document_fields(values, principal):
    for name in ('sku_ids', 'allowed_subjects', 'data_domains'):
        if name in values:
            values[name] = tuple(values[name])
    if not principal.admin and (values.get('allowed_subjects') or
            not set(values.get('data_domains', ())) <= set(principal.data_domains or ())):
        raise ForbiddenError("cannot assign access outside your own data permissions")
    return values


def stage_document(application, body, principal):
    request = IngestRequest(tenant_id=principal.tenant_id, **_document_fields(body.model_dump(), principal))
    return asdict(application.ingest(request, principal=principal))


def _image_fields(body):
    values = body.model_dump(exclude={'content_base64'}, exclude_unset=True)
    for field in ('allowed_subjects', 'tags', 'data_domains'):
        if values.get(field) is not None:
            values[field] = tuple(values[field])
    return values


def register_files(api, application, authenticate):
    User = Annotated[Principal, Depends(authenticate)]
    Kind = Literal['document', 'image']

    def decorate(tenant, rows):
        attrs = {'owner_id': 'owner_name', 'updated_by': 'updated_by_name', 'deleted_by': 'deleted_by_name'}
        nested = {'versions': {'created_by': 'created_by_name', 'updated_by': 'updated_by_name'},
                  'events': {'actor_id': 'actor_name'}}
        ids = {row[k] for row in rows for k in attrs if row.get(k)}
        ids.update(item[k] for row in rows for group, fields in nested.items()
                   for item in row.get(group, ()) for k in fields if item.get(k))
        names = api.state.accounts.display_names(tenant, ids)

        def named(item, fields):
            return item | {label: names.get(item[k], item[k]) for k, label in fields.items() if item.get(k)}

        return [named(row, attrs) | {group: [named(item, fields) for item in row[group]]
                for group, fields in nested.items() if group in row} for row in rows]

    @api.get('/files')
    def list_files(principal: User, view: Literal['all', 'mine', 'trash'] = 'mine'):
        if view == 'all' and not principal.admin:
            raise ForbiddenError('administrator permission required for the complete file list')
        # Read the pointer first: concurrent publication must conflict rather than let a
        # stale list overwrite a change using a generation obtained after listing.
        snapshot = application.store.open_snapshot(principal.tenant_id)
        files = application.store.managed_documents(principal, view) + application.assets.managed_images(principal, view)
        return {'files': decorate(principal.tenant_id, files), 'generation': snapshot.generation if snapshot else None}

    @api.post('/files/documents', status_code=201)
    def new_document(body: DocumentInput, principal: User):
        return stage_document(application, body, principal)

    @api.post('/files/pdf', status_code=201)
    def new_pdf(body: PdfUpload, principal: User):
        metadata = _document_fields(body.model_dump(exclude={'content_base64'}), principal)
        content = _decode(body.content_base64)
        if not content.startswith(b'%PDF-'):
            raise ValidationError('expected a PDF file')
        with TemporaryDirectory(prefix='rag-upload-') as directory:
            source = Path(directory) / 'upload.pdf'
            source.write_bytes(content)
            return asdict(application.ingest_pdf(source, backend='local', principal=principal,
                pdf_settings=PdfSettings(max_input_bytes=10_000_000, max_text_bytes=2_000_000),
                tenant_id=principal.tenant_id, **metadata))

    @api.post('/files/images', status_code=201)
    def new_image(body: AssetInput, principal: User):
        return application.assets.managed_create(principal, content=_decode(body.content_base64), **_image_fields(body))

    @api.patch('/files/images/{file_id}')
    def edit_image(file_id: str, body: AssetUpdate, principal: User):
        return application.assets.managed_update(principal, file_id, **_image_fields(body))

    @api.get('/files/{kind}/{file_id}')
    def file_detail(kind: Kind, file_id: str, principal: User):
        if kind == 'image':
            detail = application.assets.managed_image(principal, file_id)
        else:
            detail = application.store.managed_document(principal, file_id)
        return decorate(principal.tenant_id, [detail])[0]

    @api.get('/files/{kind}/{file_id}/content')
    def file_content(kind: Kind, file_id: str, request: Request, principal: User,
                     revision: str | None = None, original: bool = False):
        if kind == 'image':
            detail = application.assets.managed_image(principal, file_id)
            current = detail['revision_id']
            if revision is not None and revision != current:
                raise NotFoundError('image revision not found')
            content, media_type = application.assets.managed_content(principal, file_id, current)
            latest = application.assets.managed_image(authenticate(request), file_id)
            if latest['trash_id'] is not None or latest['revision_id'] != current:
                raise ConflictError('image changed during read')
        else:
            lookup = application.store.document_source
            source = lookup(principal, file_id, revision, original=original)
            content = application.objects.get(source[0])
            if lookup(authenticate(request), file_id, revision, original=original) != source:
                raise ConflictError('document changed during read')
            media_type = 'application/pdf' if source[1] == 'application/pdf' else 'text/plain'
        # Never render uploaded HTML as an active same-origin document.
        disposition = 'attachment' if media_type == 'application/pdf' else 'inline'
        return Response(content, media_type=media_type, headers={'Content-Disposition': disposition})

    @api.post('/files/{kind}/{file_id}/trash')
    def move_to_trash(kind: Kind, file_id: str, body: FileTrash, principal: User):
        if kind == 'image':
            if not body.expected_revision:
                raise ValidationError('expected_revision is required')
            return application.assets.trash_image(principal, file_id, body.expected_revision)
        return application.store.trash_document(principal, file_id, body.expected_generation)

    @api.post('/files/{kind}/{file_id}/restore')
    def restore(kind: Kind, file_id: str, body: FileRestore, principal: User):
        if kind == 'image':
            return application.assets.restore_image(principal, file_id, body.trash_id)
        return application.store.restore_document(principal, file_id, body.trash_id, body.expected_generation)

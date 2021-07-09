# * Copyright (c) 2020. Authors: see NOTICE file.
# *
# * Licensed under the Apache License, Version 2.0 (the "License");
# * you may not use this file except in compliance with the License.
# * You may obtain a copy of the License at
# *
# *      http://www.apache.org/licenses/LICENSE-2.0
# *
# * Unless required by applicable law or agreed to in writing, software
# * distributed under the License is distributed on an "AS IS" BASIS,
# * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# * See the License for the specific language governing permissions and
# * limitations under the License.
import logging
import traceback
from typing import Optional

from cytomine import Cytomine
from cytomine.models import Storage, ProjectCollection, Project, UploadedFile, ImageInstance
from fastapi import APIRouter, Query, Depends, Form, BackgroundTasks
from starlette.requests import Request
from starlette.responses import JSONResponse

from pims.api.exceptions import CytomineProblem, AuthenticationException, BadRequestException
from pims.api.utils.cytomine_auth import parse_authorization_header, parse_request_token, sign_token, \
    get_this_image_server
from pims.api.utils.image_parameter import ensure_list
from pims.api.utils.parameter import sanitize_filename
from pims.api.utils.response import serialize_cytomine_model
from pims.config import get_settings, Settings
from pims.files.file import Path
from pims.importer.importer import FileImporter
from pims.importer.listeners import CytomineListener, StdoutListener

router = APIRouter()

cytomine_logger = logging.getLogger("pims.cytomine")


@router.post('/upload', tags=['Import'])
async def legacy_import(
        request: Request,
        background: BackgroundTasks,
        core: Optional[str] = None,
        cytomine: Optional[str] = None,
        storage: Optional[int] = None,
        id_storage: Optional[int] = Query(None, alias='idStorage'),
        projects: Optional[str] = None,
        id_project: Optional[str] = Query(None, alias='idProject'),
        sync: Optional[bool] = False,
        keys: Optional[str] = None,
        values: Optional[str] = None,
        upload_name: str = Form(..., alias="files[].name"),
        upload_path: str = Form(..., alias="files[].path"),
        upload_size: int = Form(..., alias="files[].size"),
        upload_content_type: str = Form(..., alias="files[].content_type"),
        upload_md5: str = Form(..., alias="files[].md5"),
        config: Settings = Depends(get_settings)
):
    """
    Import a file (legacy)
    """
    core = cytomine if cytomine is not None else core
    if not core:
        raise BadRequestException(detail="core or cytomine parameter missing.")

    id_storage = id_storage if id_storage is not None else storage
    if not id_storage:
        raise BadRequestException(detail="idStorage or storage parameter missing.")

    projects_to_parse = id_project if id_project is not None else projects
    try:
        id_projects = []
        if projects_to_parse:
            projects = ensure_list(projects_to_parse.split(","))
            id_projects = [int(p) for p in projects]
    except ValueError:
        raise BadRequestException(detail="Invalid projects or idProject parameter.")

    public_key, signature = parse_authorization_header(request.headers)
    with Cytomine.connect(core, config.cytomine_public_key, config.cytomine_private_key) as c:
        # c._logger = cytomine_logger  # TODO: improve logging management in Python client
        if not c.current_user:
            raise AuthenticationException("PIMS authentication to Cytomine failed.")

        this = get_this_image_server(config.pims_url)
        keys = c.get("userkey/{}/keys.json".format(public_key))
        private_key = keys["privateKey"]

        if sign_token(private_key, parse_request_token(request)) != signature:
            raise AuthenticationException("Authentication to Cytomine failed")

        c.set_credentials(public_key, private_key)
        user = c.current_user
        storage = Storage().fetch(id_storage)
        if not storage:
            raise CytomineProblem("Storage {} not found".format(id_storage))

        projects = ProjectCollection()
        for pid in id_projects:
            project = Project().fetch(pid)
            if not project:
                raise CytomineProblem("Project {} not found".format(pid))
            projects.append(project)

        # TODO: keys/values

        upload_name = sanitize_filename(upload_name)
        root = UploadedFile(upload_name, upload_path, upload_size, "", upload_content_type,
                            id_projects, id_storage, user.id, this.id, UploadedFile.UPLOADED).save()

        if sync:
            try:
                root, images = _legacy_import(upload_path, upload_name, root, projects)
                return [{
                    "status": 200,
                    "name": upload_name,
                    "uploadedFile": serialize_cytomine_model(root),
                    "images": [{
                        "image": serialize_cytomine_model(image[0]),
                        "imageInstances": serialize_cytomine_model([1])
                    } for image in images]
                }]
            except Exception as e:
                traceback.print_exc()
                return JSONResponse(content=[{
                    "status": 500,
                    "error": str(e),
                    "files": [{
                        "name": upload_name,
                        "size": 0,
                        "error": str(e)
                    }]
                }], status_code=400)
        else:
            background.add_task(_legacy_import, upload_path, upload_name, root, projects)
            return JSONResponse(content=[{
                "status": 200,
                "name": upload_name,
                "uploadedFile": serialize_cytomine_model(root),
                "images": []
            }], status_code=202)


def _legacy_import(filepath, name, root_uf, projects):
    pending_file = Path(filepath)
    cytomine = CytomineListener(root_uf.id, root_uf.id)
    listeners = [
        cytomine,
        StdoutListener(name)
    ]

    fi = FileImporter(pending_file, name, listeners)
    fi.run()

    images = []
    for ai in cytomine.abstract_images:
        instances = []
        for p in projects:
            instances.append(ImageInstance(ai.id, p.id).save())
        images.append((ai, instances))

    return root_uf.fetch(), images


def import_(filepath, body):
    pass


def export(filepath):
    pass


def delete(filepath):
    pass

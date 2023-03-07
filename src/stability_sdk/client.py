#!/bin/which python3

# fmt: off

import grpc
import json
import logging
import mimetypes
import os
import random
import sys
import time
import uuid
import warnings

from argparse import ArgumentParser, Namespace
from google.protobuf.json_format import MessageToJson
from google.protobuf.struct_pb2 import Struct
from PIL import Image
from typing import Any, Dict, Generator, Iterable, List, Optional, Sequence, Tuple, Union


try:
    import numpy as np
    import pandas as pd
    import cv2 # to do: add this as an installation dependency?
except ImportError:
    warnings.warn(
        "Failed to import animation reqs. To use the animation toolchain, install the requisite dependencies via:" 
        "   pip install --upgrade stability_sdk[anim]"
    )


try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    pass
else:
    load_dotenv()

import stability_sdk.interfaces.gooseai.generation.generation_pb2 as generation
import stability_sdk.interfaces.gooseai.generation.generation_pb2_grpc as generation_grpc
import stability_sdk.interfaces.gooseai.project.project_pb2 as project
import stability_sdk.interfaces.gooseai.project.project_pb2_grpc as project_grpc

from .utils import (
    SAMPLERS,
    MAX_FILENAME_SZ,
    artifact_type_to_str,
    image_mix,
    image_to_prompt,
    open_images,
    sampler_from_string,
    tensor_to_prompt,
    truncate_fit,
)

class ClassifierException(Exception):
    """Raised when server classifies generated content as inappropriate."""
    def __init__(self, classifier_result: generation.ClassifierParameters):
        self.classifier_result = classifier_result


logger = logging.getLogger(__name__)
logger.setLevel(level=logging.INFO)


def open_channel(host: str, api_key: str = None, max_message_len: int = 10*1024*1024) -> grpc.Channel:
    options=[
        ('grpc.max_send_message_length', max_message_len),
        ('grpc.max_receive_message_length', max_message_len),
    ]    
    if host.endswith(":443"):
        call_credentials = [grpc.access_token_call_credentials(api_key)]
        channel_credentials = grpc.composite_channel_credentials(
            grpc.ssl_channel_credentials(), *call_credentials
        )
        channel = grpc.secure_channel(host, channel_credentials, options=options)
    else:
        channel = grpc.insecure_channel(host, options=options)
    return channel

def process_artifacts_from_answers(
    prefix: str,
    prompt: str,
    answers: Union[
        Generator[generation.Answer, None, None], Sequence[generation.Answer]
    ],
    write: bool = True,
    verbose: bool = False,
) -> Generator[Tuple[str, generation.Artifact], None, None]:
    """
    Process the Artifacts from the Answers.

    :param prefix: The prefix for the artifact filenames.
    :param prompt: The prompt to add to the artifact filenames.
    :param answers: The Answers to process.
    :param write: Whether to write the artifacts to disk.
    :param verbose: Whether to print the artifact filenames.
    :return: A Generator of tuples of artifact filenames and Artifacts, intended
        for passthrough.
    """
    idx = 0
    for resp in answers:
        for artifact in resp.artifacts:
            artifact_start = time.time()
            if artifact.type == generation.ARTIFACT_IMAGE:
                ext = mimetypes.guess_extension(artifact.mime)
                contents = artifact.binary
            elif artifact.type == generation.ARTIFACT_CLASSIFICATIONS:
                ext = ".pb.json"
                contents = MessageToJson(artifact.classifier).encode("utf-8")
            elif artifact.type == generation.ARTIFACT_TEXT:
                ext = ".pb.json"
                contents = MessageToJson(artifact).encode("utf-8")
            else:
                ext = ".pb"
                contents = artifact.SerializeToString()
            out_p = truncate_fit(prefix, prompt, ext, int(artifact_start), idx, MAX_FILENAME_SZ)
            if write:
                with open(out_p, "wb") as f:
                    f.write(bytes(contents))
                    if verbose:
                        artifact_t = artifact_type_to_str(artifact.type)
                        logger.info(f"wrote {artifact_t} to {out_p}")

            yield (out_p, artifact)
            idx += 1



class ApiEndpoint:
    def __init__(self, stub, engine_id):
        self.stub = stub
        self.engine_id = engine_id

class Project():
    def __init__(self, api: 'Api', project: project.Project):
        self._api = api
        self._project = project

    @property
    def id(self) -> str:
        return self._project.id

    @property
    def file_id(self) -> str:
        return self._project.file.id

    @property
    def title(self) -> str:
        return self._project.title

    @staticmethod
    def create(
        api: 'Api', 
        title: str, 
        access: project.ProjectAccess=project.PROJECT_ACCESS_PRIVATE,
        status: project.ProjectStatus=project.PROJECT_STATUS_ACTIVE
    ) -> 'Project':
        req = project.CreateProjectRequest(title=title, access=access, status=status)
        proj: project.Project = api._proj_stub.Create(req, wait_for_ready=True)
        return Project(api, proj)

    def delete(self):
        self._api._proj_stub.Delete(project.DeleteProjectRequest(id=self.id))

    @staticmethod
    def list_projects(api: 'Api') -> List['Project']:
        list_req = project.ListProjectRequest(owner_id="")
        results = []
        for proj in api._proj_stub.List(list_req, wait_for_ready=True):
            results.append(Project(api, proj))
        results.sort(key=lambda x: x.title.lower())
        return results

    def load_settings(self) -> dict:
        request = generation.Request(
            engine_id=self._api._asset.engine_id,
            prompt=[generation.Prompt(
                artifact=generation.Artifact(
                    type=generation.ARTIFACT_TEXT,
                    mime="application/json",
                    uuid=self.file_id,
                )
            )],
            asset=generation.AssetParameters(
                action=generation.ASSET_GET, 
                project_id=self.id,
                use=generation.ASSET_USE_PROJECT
            )
        )
        for resp in self._api._asset.stub.Generate(request, wait_for_ready=True):
            for artifact in resp.artifacts:
                if artifact.type == generation.ARTIFACT_TEXT:
                    return json.loads(artifact.text)
        raise Exception(f"Failed to load project file for {self.id}")

    def save_settings(self, data: dict) -> str:
        contents = json.dumps(data)
        request = generation.Request(
            engine_id=self._api._asset.engine_id,
            prompt=[generation.Prompt(
                artifact=generation.Artifact(
                    type=generation.ARTIFACT_TEXT,
                    text=contents,
                    mime="application/json",
                    uuid=self.file_id
                )
            )],
            asset=generation.AssetParameters(
                action=generation.ASSET_PUT, 
                project_id=self.id, 
                use=generation.ASSET_USE_PROJECT
            )
        )
        for resp in self._api._asset.stub.Generate(request, wait_for_ready=True):
            for artifact in resp.artifacts:
                if artifact.type == generation.ARTIFACT_TEXT:
                    self.update(file_id=artifact.uuid, file_uri=artifact.text)
                    logger.info(f"Saved project file {artifact.uuid} for {self.id}")
                    return artifact.uuid
        raise Exception(f"Failed to save project file for {self.id}")

    def put_image_asset(
        self, 
        image: Union[Image.Image, np.ndarray],
        use: generation.AssetUse=generation.ASSET_USE_OUTPUT
    ):
        store_rq = generation.Request(
            engine_id=self._api._asset.engine_id,
            prompt=[image_to_prompt(image)],
            asset=generation.AssetParameters(
                action=generation.ASSET_PUT, 
                project_id=self.id, 
                use=use
            )
        )

        for resp in self._api._asset.stub.Generate(store_rq, wait_for_ready=True):
            for artifact in resp.artifacts:
                if artifact.type == generation.ARTIFACT_TEXT:
                    return artifact.uuid
        raise Exception(f"Failed to store image asset for project {self.id}")

    def update(self, title:str=None, file_id:str=None, file_uri:str=None):
        file = project.ProjectAsset(
            id=file_id,
            uri=file_uri,
            use=project.PROJECT_ASSET_USE_PROJECT,
        ) if file_id and file_uri else None
        
        self._api._proj_stub.Update(project.UpdateProjectRequest(
            id=self.id, 
            title=title,
            file=file
        ))

        if title:
            self._project.title = title
        if file_id:
            self._project.file.id = file_id
        if file_uri:
            self._project.file.uri = file_uri

class Api:
    def __init__(self, channel: Optional[grpc.Channel]=None, stub: Optional[generation_grpc.GenerationServiceStub]=None):
        if channel is None and stub is None:
            raise Exception("Must provide either a channel or a RPC stub to Api")
        if stub is None:
            stub = generation_grpc.GenerationServiceStub(channel)
        self._proj_stub = project_grpc.ProjectServiceStub(channel) if channel else None
        self._asset = ApiEndpoint(stub, 'asset-service')
        self._generate = ApiEndpoint(stub, 'stable-diffusion-v1-5')
        self._inpaint = ApiEndpoint(stub, 'stable-inpainting-512-v2-0')
        self._interpolate = ApiEndpoint(stub, 'interpolation-server-v1')
        self._transform = ApiEndpoint(stub, 'transform-server-v1')
        self._debug_no_chains = False
        self._max_retries = 3 # retry request on RPC error
        self._retry_delay = 1.0 # base delay in seconds between retries, each attempt will double
        self._retry_obfuscation = False # retry request with different seed on classifier obfuscation
        self._retry_schedule_offset = 0.1 # increase schedule start by this amount on each retry after the first

        logger.warning(
            "\n"
            "The functionality available through this Api class is in beta and subject to changes in both functionality and pricing.\n"
            "Please be aware that these changes may affect your implementation and usage of this class.\n"
            "\n"
        )

    def generate(
        self,
        prompts: List[str], 
        weights: List[float], 
        width: int = 512, 
        height: int = 512, 
        steps: int = 50, 
        seed: Union[Sequence[int], int] = 0,
        samples: int = 1,
        cfg_scale: float = 7.0, 
        sampler: generation.DiffusionSampler = generation.SAMPLER_K_LMS,
        init_image: Optional[np.ndarray] = None,
        init_strength: float = 0.0,
        init_noise_scale: float = 1.0,
        init_depth: Optional[np.ndarray] = None,
        mask: Optional[np.ndarray] = None,
        masked_area_init: generation.MaskedAreaInit = generation.MASKED_AREA_INIT_ORIGINAL,
        mask_fixup: bool = True,
        guidance_preset: generation.GuidancePreset = generation.GUIDANCE_PRESET_NONE,
        guidance_cuts: int = 0,
        guidance_strength: float = 0.0,
        return_request: bool = False,
    ) -> Dict[int, List[Union[np.ndarray, Any]]]:
        """
        Generate an image from a set of weighted prompts.

        :param prompts: List of text prompts
        :param weights: List of prompt weights
        :param width: Width of the generated image
        :param height: Height of the generated image
        :param steps: Number of steps to run the diffusion process
        :param seed: Random seed for the starting noise
        :param samples: Number of samples to generate
        :param cfg_scale: Classifier free guidance scale
        :param sampler: Sampler to use for the diffusion process
        :param init_image: Initial image to use
        :param init_strength: Strength of the initial image
        :param init_noise_scale: Scale of the initial noise
        :param mask: Mask to use (0 for pixels to change, 255 for pixels to keep)
        :param masked_area_init: How to initialize the masked area
        :param mask_fixup: Whether to restore the unmasked area after diffusion
        :param guidance_preset: Preset to use for CLIP guidance
        :param guidance_cuts: Number of cuts to use with CLIP guidance
        :param guidance_strength: Strength of CLIP guidance
        :return: dict mapping artifact type to data
        """
        if not prompts and init_image is None:
            raise ValueError("prompt and/or init_image must be provided")

        if (mask is not None) and (init_image is None) and not return_request:
            raise ValueError("If mask_image is provided, init_image must also be provided")

        p = [generation.Prompt(text=prompt, parameters=generation.PromptParameters(weight=weight)) for prompt,weight in zip(prompts, weights)]
        if init_image is not None:
            p.append(image_to_prompt(init_image))
        if mask is not None:
            p.append(image_to_prompt(mask, type=generation.ARTIFACT_MASK))
        if init_depth is not None:
            p.append(image_to_prompt(init_depth, type=generation.ARTIFACT_DEPTH))

        start_schedule = 1.0 - init_strength
        image_params = self._build_image_params(width, height, sampler, steps, seed, samples, cfg_scale, 
                                                start_schedule, init_noise_scale, masked_area_init, 
                                                guidance_preset, guidance_cuts, guidance_strength)

        request = generation.Request(engine_id=self._generate.engine_id, prompt=p, image=image_params)
        if return_request:
            return request

        results = self._run_request(self._generate, request)

        # optionally force pixels in unmasked areas not to change
        if init_image is not None and mask is not None and mask_fixup:
            results[generation.ARTIFACT_IMAGE] = [image_mix(image, init_image, mask) for image in results[generation.ARTIFACT_IMAGE]]

        return results

    def inpaint(
        self,
        image: np.ndarray,
        mask: np.ndarray,
        prompts: List[str], 
        weights: List[float], 
        steps: int = 50, 
        seed: Union[Sequence[int], int] = 0,
        samples: int = 1,
        cfg_scale: float = 7.0, 
        sampler: generation.DiffusionSampler = generation.SAMPLER_K_LMS,
        init_strength: float = 0.0,
        init_noise_scale: float = 1.0,
        masked_area_init: generation.MaskedAreaInit = generation.MASKED_AREA_INIT_ZERO,
        mask_fixup: bool = False,
        guidance_preset: generation.GuidancePreset = generation.GUIDANCE_PRESET_NONE,
        guidance_cuts: int = 0,
        guidance_strength: float = 0.0,
    ) -> Dict[int, List[Union[np.ndarray, Any]]]:
        """
        Apply inpainting to an image.
        
        :param image: Source image
        :param mask: Mask image with 0 for pixels to change and 255 for pixels to keep
        :param prompts: List of text prompts
        :param weights: List of prompt weights
        :param steps: Number of steps to run
        :param seed: Random seed
        :param samples: Number of samples to generate
        :param cfg_scale: Classifier free guidance scale        
        :param sampler: Sampler to use for the diffusion process
        :param init_strength: Strength of the initial image
        :param init_noise_scale: Scale of the initial noise
        :param masked_area_init: How to initialize the masked area
        :param mask_fixup: Whether to restore the unmasked area after diffusion
        :param guidance_preset: Preset to use for CLIP guidance
        :param guidance_cuts: Number of cuts to use with CLIP guidance
        :param guidance_strength: Strength of CLIP guidance
        :return: dict mapping artifact type to data
        """
        width, height = image.shape[1], image.shape[0]

        p = [generation.Prompt(text=prompt, parameters=generation.PromptParameters(weight=weight)) for prompt,weight in zip(prompts, weights)]
        if image is not None:
            p.append(image_to_prompt(image))
            if mask is not None:
                p.append(image_to_prompt(mask, type=generation.ARTIFACT_MASK))

        start_schedule = 1.0-init_strength
        image_params = self._build_image_params(width, height, sampler, steps, seed, samples, cfg_scale, 
                                                start_schedule, init_noise_scale, masked_area_init, 
                                                guidance_preset, guidance_cuts, guidance_strength)

        request = generation.Request(engine_id=self._inpaint.engine_id, prompt=p, image=image_params)        
        results = self._run_request(self._inpaint, request)

        # optionally force pixels in unmasked areas not to change
        if mask_fixup:
            results[generation.ARTIFACT_IMAGE] = [image_mix(res_image, image, mask) for res_image in results[generation.ARTIFACT_IMAGE]]

        return results

    def interpolate(
        self,
        images: Iterable[np.ndarray], 
        ratios: List[float],
        mode: generation.InterpolateMode = generation.INTERPOLATE_LINEAR,
    ) -> List[np.ndarray]:
        """
        Interpolate between two images

        :param images: Two images with matching resolution
        :param ratios: In-between ratios to interpolate at
        :param mode: Interpolation mode
        :return: One image for each ratio
        """
        assert len(images) == 2
        assert len(ratios) >= 1

        if len(ratios) == 1:
            if ratios[0] == 0.0:
                return [images[0]]
            elif ratios[0] == 1.0:
                return [images[1]]
            elif mode == generation.INTERPOLATE_LINEAR:
               return [image_mix(images[0], images[1], ratios[0])]

        p = [image_to_prompt(image) for image in images]
        request = generation.Request(
            engine_id=self._interpolate.engine_id,
            prompt=p,
            interpolate=generation.InterpolateParameters(ratios=ratios, mode=mode)
        )

        results = self._run_request(self._interpolate, request)
        return results[generation.ARTIFACT_IMAGE]

    def transform_and_generate(
        self,
        image: np.ndarray,
        params: List[generation.TransformParameters],
        generate_request: generation.Request,
        extras: Optional[Dict] = None,
    ) -> np.ndarray:
        extras_struct = None
        if extras is not None:
            extras_struct = Struct()
            extras_struct.update(extras)

        if not params:
            results = self._run_request(self._generate, generate_request)
            return results[generation.ARTIFACT_IMAGE][0]

        requests = [
            generation.Request(
                engine_id=self._transform.engine_id,
                requested_type=generation.ARTIFACT_TENSOR,
                prompt=[image_to_prompt(image)],
                transform=param,
                extras=extras_struct,
            ) for param in params
        ]

        if self._debug_no_chains:
            prev_result = None
            for rq in requests:
                if prev_result is not None:
                    rq.prompt.pop()
                    rq.prompt.append(tensor_to_prompt(prev_result))
                prev_result = self._run_request(self._transform, rq)[generation.ARTIFACT_TENSOR][0]
            generate_request.prompt.append(tensor_to_prompt(prev_result))
            results = self._run_request(self._generate, generate_request)
        else:
            stages = []
            for idx, rq in enumerate(requests):
                stages.append(generation.Stage(
                    id=str(idx),
                    request=rq, 
                    on_status=[generation.OnStatus(
                        action=[generation.STAGE_ACTION_PASS], 
                        target=str(idx+1)
                    )]
                ))
            stages.append(generation.Stage(
                id=str(len(params)),
                request=generate_request,
                on_status=[generation.OnStatus(
                    action=[generation.STAGE_ACTION_RETURN],
                    target=None
                )]
            ))
            chain_rq = generation.ChainRequest(request_id="xform_gen_chain", stage=stages)
            results = self._run_request(self._transform, chain_rq)

        return results[generation.ARTIFACT_IMAGE][0]

    def transform(
        self,
        images: Iterable[np.ndarray],
        params: Union[generation.TransformParameters, List[generation.TransformParameters]],
        extras: Optional[Dict] = None
    ) -> Tuple[List[np.ndarray], Optional[List[np.ndarray]]]:
        """
        Transform images

        :param images: One or more images to transform
        :param params: Transform operations to apply to each image
        :return: One image artifact for each image and one transform dependent mask
        """
        assert len(images)
        assert isinstance(images[0], np.ndarray)

        extras_struct = None
        if extras is not None:
            extras_struct = Struct()
            extras_struct.update(extras)

        if isinstance(params, List) and len(params) > 1:
            if self._debug_no_chains:
                for param in params:
                    images, mask = self.transform(images, param, extras)
                return images, mask

            assert extras is None
            stages = []
            for idx, param in enumerate(params):
                final = idx == len(params) - 1
                rq = generation.Request(
                    engine_id=self._transform.engine_id,
                    prompt=[image_to_prompt(image) for image in images] if idx == 0 else None,
                    transform=param,
                    extras_struct=extras_struct
                )
                stages.append(generation.Stage(
                    id=str(idx),
                    request=rq, 
                    on_status=[generation.OnStatus(
                        action=[generation.STAGE_ACTION_PASS if not final else generation.STAGE_ACTION_RETURN], 
                        target=str(idx+1) if not final else None
                    )]
                ))
            chain_rq = generation.ChainRequest(request_id="xform_chain", stage=stages)
            results = self._run_request(self._transform, chain_rq)
        else:
            request = generation.Request(
                engine_id=self._transform.engine_id,
                prompt=[image_to_prompt(image) for image in images],
                transform=params[0] if isinstance(params, List) else params,
                extras=extras_struct
            )
            results = self._run_request(self._transform, request)

        images = results.get(generation.ARTIFACT_IMAGE, []) + results.get(generation.ARTIFACT_DEPTH, [])
        masks = results.get(generation.ARTIFACT_MASK, None)
        return images, masks

    # TODO: Add option to do transform using given depth map (e.g. for Blender use cases)
    def transform_3d(
        self, 
        images: Iterable[np.ndarray], 
        depth_calc: generation.TransformParameters,
        transform: generation.TransformParameters,
        extras: Optional[Dict] = None
    ) -> Tuple[List[np.ndarray], Optional[List[np.ndarray]]]:
        assert len(images)
        assert isinstance(images[0], np.ndarray)

        image_prompts = [image_to_prompt(image) for image in images]
        warped_images = []
        warp_mask = None
        op_id = "resample" if transform.HasField("resample") else "camera_pose"

        extras_struct = Struct()
        if extras is not None:
            extras_struct.update(extras)

        rq_depth = generation.Request(
            engine_id=self._transform.engine_id,
            requested_type=generation.ARTIFACT_TENSOR,
            prompt=[image_prompts[0]],
            transform=depth_calc,
        )
        rq_transform = generation.Request(
            engine_id=self._transform.engine_id,
            prompt=image_prompts,
            transform=transform,
            extras=extras_struct
        )

        if self._debug_no_chains:
            results = self._process_response(self._transform.stub.Generate(rq_depth, wait_for_ready=True))
            rq_transform.prompt.append(
                generation.Prompt(
                    artifact=generation.Artifact(
                        type=generation.ARTIFACT_TENSOR,
                        tensor=results[generation.ARTIFACT_TENSOR][0]
                    )
                )
            )
            results = self._run_request(self._transform, rq_transform)
        else:
            chain_rq = generation.ChainRequest(
                request_id=f"{op_id}_3d_chain",
                stage=[
                    generation.Stage(
                        id="depth_calc",
                        request=rq_depth,
                        on_status=[generation.OnStatus(action=[generation.STAGE_ACTION_PASS], target=op_id)]
                    ),
                    generation.Stage(
                        id=op_id,
                        request=rq_transform,
                        on_status=[generation.OnStatus(action=[generation.STAGE_ACTION_RETURN])]
                    ) 
                ])
            results = self._run_request(self._transform, chain_rq)

        warped_images = results[generation.ARTIFACT_IMAGE]
        warp_mask = results.get(generation.ARTIFACT_MASK, None)

        return warped_images, warp_mask

    def _adjust_request_for_retry(self, request: generation.Request, attempt: int):
        logger.warning(f"  adjusting request, will retry {self._max_retries-attempt} more times")
        request.image.seed[:] = [seed + 1 for seed in request.image.seed]
        if attempt > 0 and request.image.parameters and request.image.parameters[0].HasField("schedule"):
            schedule = request.image.parameters[0].schedule
            if schedule.HasField("start"):
                schedule.start = max(0.0, min(1.0, schedule.start + self._retry_schedule_offset))

    def _build_image_params(self, width, height, sampler, steps, seed, samples, cfg_scale, 
                            schedule_start, init_noise_scale, masked_area_init, 
                            guidance_preset, guidance_cuts, guidance_strength):

        if not seed:
            seed = [random.randrange(0, 4294967295)]
        elif isinstance(seed, int):
            seed = [seed]
        else:
            seed = list(seed)

        step_parameters = {
            "scaled_step": 0,
            "sampler": generation.SamplerParameters(cfg_scale=cfg_scale, init_noise_scale=init_noise_scale),
        }
        if schedule_start != 1.0:
            step_parameters["schedule"] = generation.ScheduleParameters(start=schedule_start)

        if guidance_preset is not generation.GUIDANCE_PRESET_NONE:
            cutouts = generation.CutoutParameters(count=guidance_cuts) if guidance_cuts else None
            if guidance_strength == 0.0:
                guidance_strength = None
            step_parameters["guidance"] = generation.GuidanceParameters(
                guidance_preset=guidance_preset,
                instances=[
                    generation.GuidanceInstanceParameters(
                        cutouts=cutouts,
                        guidance_strength=guidance_strength,
                        models=None, prompt=None
                    )
                ]
            )

        return generation.ImageParameters(
            transform=generation.TransformType(diffusion=sampler),
            height=height,
            width=width,
            seed=seed,
            steps=steps,
            samples=samples,
            masked_area_init=masked_area_init,
            parameters=[generation.StepParameter(**step_parameters)],
        )

    def _process_response(self, response) -> Dict[int, List[np.ndarray]]:
        results: Dict[int, List[np.ndarray]] = {}
        for resp in response:
            for artifact in resp.artifacts:
                if artifact.type not in results:
                    results[artifact.type] = []
                if artifact.type == generation.ARTIFACT_CLASSIFICATIONS:
                    results[artifact.type].append(artifact.classifier)
                elif artifact.type in (generation.ARTIFACT_DEPTH, generation.ARTIFACT_IMAGE, generation.ARTIFACT_MASK):
                    nparr = np.frombuffer(artifact.binary, np.uint8)
                    im = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                    results[artifact.type].append(im)
                elif artifact.type == generation.ARTIFACT_TENSOR:
                    results[artifact.type].append(artifact.tensor)
                elif artifact.type == generation.ARTIFACT_TEXT:
                    results[artifact.type].append(artifact.text)
        return results

    def _run_request(
        self, 
        endpoint: ApiEndpoint, 
        request: Union[generation.ChainRequest, generation.Request]
    ) -> Dict[int, List[Union[np.ndarray, Any]]]:
        for attempt in range(self._max_retries+1):
            try:
                if isinstance(request, generation.Request):
                    assert endpoint.engine_id == request.engine_id
                    response = endpoint.stub.Generate(request, wait_for_ready=True)
                else:
                    response = endpoint.stub.ChainGenerate(request, wait_for_ready=True)

                results = self._process_response(response)

                # check for classifier obfuscation
                if generation.ARTIFACT_CLASSIFICATIONS in results:
                    for classifier in results[generation.ARTIFACT_CLASSIFICATIONS]:
                        if classifier.realized_action == generation.ACTION_OBFUSCATE:
                            raise ClassifierException(classifier)

                break
            except ClassifierException as ce:
                if attempt == self._max_retries or not self._retry_obfuscation:
                    raise ce
                
                for exceed in ce.classifier_result.exceeds:
                    logger.warning(f"Received classifier obfuscation. Exceeded {exceed.name} threshold")
                    for concept in exceed.concepts:
                        if concept.HasField("threshold"):
                            logger.warning(f"  {concept.concept} ({concept.threshold})")
                
                if isinstance(request, generation.Request) and request.HasField("image"):
                    self._adjust_request_for_retry(request, attempt)
                elif isinstance(request, generation.ChainRequest):
                    for stage in request.stage:
                        if stage.request.HasField("image"):
                            self._adjust_request_for_retry(stage.request, attempt)
                else:
                    raise ce
            except grpc.RpcError as rpc_error:
                if attempt == self._max_retries:
                    raise rpc_error
                logger.warning(f"Received RpcError: {rpc_error} will retry {self._max_retries-attempt} more times")
                time.sleep(self._retry_delay * 2**attempt)
        return results

class StabilityInference:
    def __init__(
        self,
        host: str = "grpc.stability.ai:443",
        key: str = "",
        engine: str = "stable-diffusion-v1-5",
        verbose: bool = False,
        wait_for_ready: bool = True,
    ):
        """
        Initialize the client.

        :param host: Host to connect to.
        :param key: Key to use for authentication.
        :param engine: Engine to use.
        :param verbose: Whether to print debug messages.
        :param wait_for_ready: Whether to wait for the server to be ready, or
            to fail immediately.
        """
        self.verbose = verbose
        self.engine = engine
        self.grpc_args = {"wait_for_ready": wait_for_ready}
        if verbose:
            logger.info(f"Opening channel to {host}")
        self.stub = generation_grpc.GenerationServiceStub(open_channel(host=host, api_key=key))


    def generate(
        self,
        prompt: Union[str, List[str], generation.Prompt, List[generation.Prompt]],
        init_image: Optional[Image.Image] = None,
        mask_image: Optional[Image.Image] = None,
        height: int = 512,
        width: int = 512,
        start_schedule: float = 1.0,
        end_schedule: float = 0.01,
        cfg_scale: float = 7.0,
        sampler: generation.DiffusionSampler = generation.SAMPLER_K_LMS,
        steps: int = 50,
        seed: Union[Sequence[int], int] = 0,
        samples: int = 1,
        safety: bool = True,
        classifiers: Optional[generation.ClassifierParameters] = None,
        guidance_preset: generation.GuidancePreset = generation.GUIDANCE_PRESET_NONE,
        guidance_cuts: int = 0,
        guidance_strength: Optional[float] = None,
        guidance_prompt: Union[str, generation.Prompt] = None,
        guidance_models: List[str] = None,
    ) -> Generator[generation.Answer, None, None]:
        """
        Generate images from a prompt.

        :param prompt: Prompt to generate images from.
        :param init_image: Init image.
        :param mask_image: Mask image
        :param height: Height of the generated images.
        :param width: Width of the generated images.
        :param start_schedule: Start schedule for init image.
        :param end_schedule: End schedule for init image.
        :param cfg_scale: Scale of the configuration.
        :param sampler: Sampler to use.
        :param steps: Number of steps to take.
        :param seed: Seed for the random number generator.
        :param samples: Number of samples to generate.
        :param safety: DEPRECATED/UNUSED - Cannot be disabled.
        :param classifiers: DEPRECATED/UNUSED - Has no effect on image generation.
        :param guidance_preset: Guidance preset to use. See generation.GuidancePreset for supported values.
        :param guidance_cuts: Number of cuts to use for guidance.
        :param guidance_strength: Strength of the guidance. We recommend values in range [0.0,1.0]. A good default is 0.25
        :param guidance_prompt: Prompt to use for guidance, defaults to `prompt` argument (above) if not specified.
        :param guidance_models: Models to use for guidance.
        :return: Generator of Answer objects.
        """
        if (prompt is None) and (init_image is None):
            raise ValueError("prompt and/or init_image must be provided")

        if (mask_image is not None) and (init_image is None):
            raise ValueError(
                "If mask_image is provided, init_image must also be provided"
            )

        if not seed:
            seed = [random.randrange(0, 4294967295)]
        elif isinstance(seed, int):
            seed = [seed]
        else:
            seed = list(seed)

        prompts: List[generation.Prompt] = []
        if any(isinstance(prompt, t) for t in (str, generation.Prompt)):
            prompt = [prompt]
        for p in prompt:
            if isinstance(p, str):
                p = generation.Prompt(text=p)
            elif not isinstance(p, generation.Prompt):
                raise TypeError("prompt must be a string or generation.Prompt object")
            prompts.append(p)

        step_parameters = dict(
            scaled_step=0,
            sampler=generation.SamplerParameters(cfg_scale=cfg_scale),
        )
            
        # NB: Specifying schedule when there's no init image causes washed out results
        if init_image is not None:
            step_parameters['schedule'] = generation.ScheduleParameters(
                start=start_schedule,
                end=end_schedule,
            )
            prompts += [image_to_prompt(init_image)]

            if mask_image is not None:
                prompts += [image_to_prompt(mask_image, type=generation.ARTIFACT_MASK)]

        
        if guidance_prompt:
            if isinstance(guidance_prompt, str):
                guidance_prompt = generation.Prompt(text=guidance_prompt)
            elif not isinstance(guidance_prompt, generation.Prompt):
                raise ValueError("guidance_prompt must be a string or Prompt object")
        if guidance_strength == 0.0:
            guidance_strength = None

            
        # Build our CLIP parameters
        if guidance_preset is not generation.GUIDANCE_PRESET_NONE:
            # to do: make it so user can override this
            step_parameters['sampler']=None

            if guidance_models:
                guiders = [generation.Model(alias=model) for model in guidance_models]
            else:
                guiders = None

            if guidance_cuts:
                cutouts = generation.CutoutParameters(count=guidance_cuts)
            else:
                cutouts = None

            step_parameters["guidance"] = generation.GuidanceParameters(
                guidance_preset=guidance_preset,
                instances=[
                    generation.GuidanceInstanceParameters(
                        guidance_strength=guidance_strength,
                        models=guiders,
                        cutouts=cutouts,
                        prompt=guidance_prompt,
                    )
                ],
            )

        image_parameters=generation.ImageParameters(
            transform=generation.TransformType(diffusion=sampler),
            height=height,
            width=width,
            seed=seed,
            steps=steps,
            samples=samples,
            parameters=[generation.StepParameter(**step_parameters)],
        )

        return self.emit_request(prompt=prompts, image_parameters=image_parameters)

            
    # The motivation here is to facilitate constructing requests by passing protobuf objects directly.
    def emit_request(
        self,
        prompt: generation.Prompt,
        image_parameters: generation.ImageParameters,
        engine_id: str = None,
        request_id: str = None,
    ):
        if not request_id:
            request_id = str(uuid.uuid4())
        if not engine_id:
            engine_id = self.engine
        
        rq = generation.Request(
            engine_id=engine_id,
            request_id=request_id,
            prompt=prompt,
            image=image_parameters
        )
        
        if self.verbose:
            logger.info("Sending request.")

        start = time.time()
        for answer in self.stub.Generate(rq, **self.grpc_args):
            duration = time.time() - start
            if self.verbose:
                if len(answer.artifacts) > 0:
                    artifact_ts = [
                        artifact_type_to_str(artifact.type)
                        for artifact in answer.artifacts
                    ]
                    logger.info(
                        f"Got {answer.answer_id} with {artifact_ts} in "
                        f"{duration:0.2f}s"
                    )
                else:
                    logger.info(
                        f"Got keepalive {answer.answer_id} in " f"{duration:0.2f}s"
                    )

            yield answer
            start = time.time()


if __name__ == "__main__":
    # Set up logging for output to console.
    fh = logging.StreamHandler()
    fh_formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(filename)s(%(process)d) - %(message)s"
    )
    fh.setFormatter(fh_formatter)
    logger.addHandler(fh)

    logger.warning(
        "[Deprecation Warning] The method you have used to invoke the sdk will be deprecated shortly."
        "[Deprecation Warning] Please modify your code to call the sdk without invoking the 'client' module instead."
        "[Deprecation Warning] rather than:"
        "[Deprecation Warning]    $ python -m stability_sdk.client ...  "
        "[Deprecation Warning] instead do this:"
        "[Deprecation Warning]    $ python -m stability_sdk ...  "
    )
    
    STABILITY_HOST = os.getenv("STABILITY_HOST", "grpc.stability.ai:443")
    STABILITY_KEY = os.getenv("STABILITY_KEY", "")

    if not STABILITY_HOST:
        logger.warning("STABILITY_HOST environment variable needs to be set.")
        sys.exit(1)

    if not STABILITY_KEY:
        logger.warning(
            "STABILITY_KEY environment variable needs to be set. You may"
            " need to login to the Stability website to obtain the"
            " API key."
        )
        sys.exit(1)

    # CLI parsing
    parser = ArgumentParser()
    parser.add_argument(
        "--height", "-H", type=int, default=512, help="[512] height of image"
    )
    parser.add_argument(
        "--width", "-W", type=int, default=512, help="[512] width of image"
    )
    parser.add_argument(
        "--start_schedule",
        type=float,
        default=0.5,
        help="[0.5] start schedule for init image (must be greater than 0, 1 is full strength text prompt, no trace of image)",
    )
    parser.add_argument(
        "--end_schedule",
        type=float,
        default=0.01,
        help="[0.01] end schedule for init image",
    )
    parser.add_argument(
        "--cfg_scale", "-C", type=float, default=7.0, help="[7.0] CFG scale factor"
    )
    parser.add_argument(
        "--sampler",
        "-A",
        type=str,        
        help="[auto-select] (" + ", ".join(SAMPLERS.keys()) + ")",
    )
    parser.add_argument(
        "--steps", "-s", type=int, default=None, help="[auto] number of steps"
    )
    parser.add_argument("--seed", "-S", type=int, default=0, help="random seed to use")
    parser.add_argument(
        "--prefix",
        "-p",
        type=str,
        default="generation_",
        help="output prefixes for artifacts",
    )
    parser.add_argument(
        "--no-store", action="store_true", help="do not write out artifacts"
    )
    parser.add_argument(
        "--num_samples", "-n", type=int, default=1, help="number of samples to generate"
    )
    parser.add_argument("--show", action="store_true", help="open artifacts using PIL")
    parser.add_argument(
        "--engine",
        "-e",
        type=str,
        help="engine to use for inference",
        default="stable-diffusion-v1-5",
    )
    parser.add_argument(
        "--init_image",
        "-i",
        type=str,
        help="Init image",
    )
    parser.add_argument(
        "--mask_image",
        "-m",
        type=str,
        help="Mask image",
    )
    parser.add_argument("prompt", nargs="*")

    args = parser.parse_args()
    if not args.prompt and not args.init_image:
        logger.warning("prompt or init image must be provided")
        parser.print_help()
        sys.exit(1)
    else:
        args.prompt = " ".join(args.prompt)

    if args.init_image:
        args.init_image = Image.open(args.init_image)

    if args.mask_image:
        args.mask_image = Image.open(args.mask_image)

    request =  {
        "height": args.height,
        "width": args.width,
        "start_schedule": args.start_schedule,
        "end_schedule": args.end_schedule,
        "cfg_scale": args.cfg_scale,                
        "seed": args.seed,
        "samples": args.num_samples,
        "init_image": args.init_image,
        "mask_image": args.mask_image,
    }

    if args.sampler:
        request["sampler"] = sampler_from_string(args.sampler)

    if args.steps:
        request["steps"] = args.steps

    stability_api = StabilityInference(
        STABILITY_HOST, STABILITY_KEY, engine=args.engine, verbose=True
    )

    answers = stability_api.generate(args.prompt, **request)
    artifacts = process_artifacts_from_answers(
        args.prefix, args.prompt, answers, write=not args.no_store, verbose=True
    )
    if args.show:
        for artifact in open_images(artifacts, verbose=True):
            pass
    else:
        for artifact in artifacts:
            pass

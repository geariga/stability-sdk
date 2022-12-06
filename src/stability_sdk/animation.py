import bisect
import cv2
import json
import logging
import math
import numpy as np
import os
import param
import random

from collections import OrderedDict
from PIL import Image
from types import SimpleNamespace
from typing import Generator, List, Optional, Tuple

from stability_sdk.client import (
    Api,
    generation,
    image_mix
)


import stability_sdk.matrix as matrix

from stability_sdk.utils import (
    blend_op,
    border_mode_from_str_2d,
    border_mode_from_str_3d,
    colormatch_op,
    contrast_op,
    depthcalc_op,
    guidance_from_string,
    interp_mode_from_str,
    key_frame_inbetweens,
    key_frame_parse,
    sampler_from_string,
    warp2d_op,
    warp3d_op,
    warpflow_op,
)

logger = logging.getLogger(__name__)
logger.setLevel(level=logging.INFO)


docstring_bordermode = ( 
    "Method that will be used to fill empty regions, e.g. after a rotation transform."
    "\n\t* reflect - Mirror pixels across the image edge to fill empty regions."
    "\n\t* replicate - Use closest pixel values (default)."
    "\n\t* wrap - Treat image borders as if they were connected, i.e. use pixels from left edge to fill empty regions touching the right edge."
    "\n\t* zero - Fill empty regions with black pixels."
)

# to do: these defaults and bounds should be configured in a language agnostic way so they can be 
# shared across client libraries, front end, etc.
# https://param.holoviz.org/user_guide/index.html
# TO DO: "prompt" argument has a bit of a logical collision with animation prompts
class BasicSettings(param.Parameterized):
    #prompt  = param.String(default="A beautiful painting of yosemite national park, by Neil Gaiman", doc="A string")
    height = param.Integer(default=512, doc="Output image dimensions. Will be resized to a multiple of 64.")
    width = param.Integer(default=512, doc="Output image dimensions. Will be resized to a multiple of 64.")
    sampler = param.ObjectSelector(default='K_euler_ancestral', objects=["DDIM", "PLMS", "K_euler", "K_euler_ancestral", "K_heun", "K_dpm_2", "K_dpm_2_ancestral", "K_lms"])
    model = param.ObjectSelector(default='stable-diffusion-v1-5', objects=["stable-diffusion-v1-5", "stable-diffusion-512-v2-0", "stable-diffusion-768-v2-0"])
    seed = param.Integer(default=-1, doc="Provide a seed value for more deterministic behavior. Negative seed values will be replaced with a random seed (default).")
    cfg_scale = param.Number(default=7, softbounds=(0,20), doc="Classifier-free guidance scale. Strength of prompt influence on denoising process. `cfg_scale=0` gives unconditioned sampling.")
    clip_guidance = param.ObjectSelector(default='FastBlue', objects=["None", "Simple", "FastBlue", "FastGreen"], doc="CLIP-guidance preset.")
    init_image = param.String(default='', doc="Path to image. Height and width dimensions will be inherited from image.")
    init_sizing = param.ObjectSelector(default='stretch', objects=["cover", "stretch", "resize-canvas"])
    steps_strength_adj = param.Boolean(default=True, doc="Adjusts number of diffusion steps based on current previous frame strength value.")    
    mask_path = param.String(default="", doc="Path to image or video mask")
    mask_invert = param.Boolean(default=False, doc="White in mask marks areas to change by default.")
    ####
    # missing param: n_samples = param.Integer(1, bounds=(1,9))

class AnimationSettings(param.Parameterized):
    animation_mode = param.ObjectSelector(default='3D', objects=['2D', '3D', 'Video Input'])
    max_frames = param.Integer(default=60, doc="Force stop of animation job after this many frames are generated.")
    border = param.ObjectSelector(default='replicate', objects=['reflect', 'replicate', 'wrap', 'zero'], doc=docstring_bordermode)
    inpaint_border = param.Boolean(default=False, doc="Use inpainting on top of border regions. Defaults to False")
    interpolate_prompts = param.Boolean(default=False, doc="Smoothly interpolate prompts between keyframes. Defaults to False")
    locked_seed = param.Boolean(default=False)


# TO DO: ability to specify backfill/interpolation method for each parameter
# TO DO: ability to provide a function that returns a parameter value given some frame index
# TO DO: inherit from param.String to add validation to these things
# TO DO: should defaults be noop or opinionated? Maybe a separate object for opinionated defaults?
class KeyframedSettings(param.Parameterized):
    """
    See disco/deforum keyframing syntax, originally developed by Chigozie Nri
    General syntax: "<frameId>:(<valueAtFrame>), f2:(v2),f3:(v3)...." 
    Values between intermediate keyframes will be linearly interpolated by default to produce smooth transitions.
    For abrupt transitions, specify values at adjacent keyframes.
    """
    angle = param.String(default="0:(1)")
    zoom = param.String(default="0:(1)")
    translation_x = param.String(default="0:(0)")
    translation_y = param.String(default="0:(0)")
    translation_z = param.String(default="0:(1)")
    rotation_x = param.String(default="0:(0)", doc="Euler angle in radians")
    rotation_y = param.String(default="0:(0)", doc="Euler angle in radians")
    rotation_z = param.String(default="0:(0)", doc="Euler angle in radians")
    brightness_curve = param.String(default="0:(1.0)")
    contrast_curve = param.String(default="0:(1.0)")
    noise_curve = param.String(default="0:(0.0)")
    noise_scale_curve = param.String(default="0:(1.02)")
    steps_curve = param.String(default="0:(50)", doc="Diffusion steps")
    strength_curve = param.String(default="0:(0.65)", doc="Image Strength (of init image relative to the prompt). 0 for ignore init image and attend only to prompt, 1 would return the init image unmodified")


# should diffusion cadence be moved up to the keyframed settings?
# if not, maybe stuff like steps and strength should be moved elsewhere?
class CoherenceSettings(param.Parameterized):
    color_coherence = param.ObjectSelector(default='LAB', objects=['None', 'HSV', 'LAB', 'RGB'], doc="Color space that will be used for inter-frame color adjustments.")
    diffusion_cadence_curve = param.String(default="0:(4)", doc="One greater than the number of frames between diffusion operations. A cadence of 1 performs diffusion on each frame. Values greater than one will generate frames using interpolation methods.")
    accumulate_xforms = param.Boolean(default=True)
    cadence_interp = param.ObjectSelector(default='mix', objects=['mix', 'rife', 'vae-lerp', 'vae-slerp'])


# TO DO: change to a generic `depth_weight` rather than specifying model name in the parameter
class DepthwarpSettings(param.Parameterized):
    #use_depth_warping = True #@param {type:"boolean"}
    midas_weight = param.Number(default=0.3, softbounds=(0,1), doc="Strength of depth model influence.")
    near_plane = param.Number(default=200, doc="Distance to nearest plane of camera view volume.")
    far_plane = param.Number(default=10000, doc="Distance to furthest plane of camera view volume.")
    fov_curve = param.String(default="0:(25)", doc="FOV angle of camera volume in degrees.")
    save_depth_maps = param.Boolean(default=False)


class VideoInputSettings(param.Parameterized):
    video_init_path = param.String(default="", doc="Path to video input")
    extract_nth_frame = param.Integer(default=1, bounds=(1,None), doc="Only use every Nth frame of the video")
    video_mix_in_curve = param.String(default="0:(0.02)")
    video_flow_warp = param.Boolean(default=True, doc="Whether or not to transfer the optical flow from the video to the generated animation as a warp effect.")


class AnimationArgs(
    BasicSettings,
    AnimationSettings,
    KeyframedSettings,
    CoherenceSettings,
    DepthwarpSettings,
    VideoInputSettings,
):
    pass


def args2dict(args):
    """
    Converts arguments object to an OrderedDict
    """
    f = None
    if isinstance(args, param.Parameterized):
        f = args2dict_param
    if isinstance(args, SimpleNamespace):
        f = args2dict_simplenamespace
    if f is None:
        raise NotImplementedError(f"Unsupported arguments object type: {type(args)}")
    return f(args)

def args2dict_simplenamespace(args):
    return OrderedDict(vars(args))

def args2dict_param(args):
    return OrderedDict(args.param.values())

def cv2_to_pil(img):
    assert img is not None
    return Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))


def make_xform_2dm(
    w: float, h: float,
    rotate: float, # in radians
    scale: float,
    translate_x: float,
    translate_y: float,
) -> matrix.Matrix:
    center = (w / 2, h / 2)
    pre = matrix.translation(-center[0], -center[1], 0)
    post = matrix.translation(center[0], center[1], 0)
    rotate = matrix.rotation_euler(0, 0, rotate)
    scale = matrix.scale(scale, scale, 1)
    rotate_scale = matrix.multiply(post, matrix.multiply(rotate, matrix.multiply(scale, pre)))
    translate = matrix.translation(translate_x, translate_y, 0)
    return matrix.multiply(rotate_scale, translate)


def to_3x3(m: matrix.Matrix) -> matrix.Matrix:
    # convert 4x4 matrix with 2D rotation, scale, and translation to 3x3 matrix
    return [[m[0][0], m[0][1], m[0][3]],
            [m[1][0], m[1][1], m[1][3]],
            [m[3][0], m[3][1], m[3][3]]]

class Animator:
    def __init__(
        self,
        api: Api,
        animation_prompts,
        args=None,
        out_dir='.',
        negative_prompt='',
        negative_prompt_weight=-1.0,
        resume: bool = False
    ):
        self.api = api
        self.animation_prompts = animation_prompts
        self.args = args
        self.color_match_image: Optional[np.ndarray] = None
        self.diffusion_cadence_ofs: int = 0
        self.frame_args = None
        self.keyframe_values: List[int] = None
        self.out_dir: str = out_dir
        self.mask: Optional[np.ndarray] = None
        self.mask_reader = None
        self.prior_frames: List[np.ndarray] = []    # forward warped prior frames
        self.prior_diffused: List[np.ndarray] = []  # results of diffusion
        self.prior_xforms: List[matrix.Matrix] = []    # accumulated transforms since last diffusion
        self.negative_prompt: str = negative_prompt
        self.negative_prompt_weight: float = negative_prompt_weight
        self.start_frame_idx: int = 0
        self.video_prev_frame: Optional[np.ndarray] = None
        self.video_reader = None

        self.setup_animation(resume)

    def apply_inpainting(self, mask: np.ndarray, prompts: List[str], weights: List[float], inpaint_steps: int, seed: int):
        for i in range(len(self.prior_frames)):
            self.prior_frames[i] = self.api.inpaint(
                image=self.prior_frames[i],
                mask=mask,
                prompts=prompts,
                weights=weights,
                steps=inpaint_steps,
                seed=seed,
                cfg_scale=self.args.cfg_scale,
                blur_radius=5,
            )

    def generate_depth_image(self, image: np.ndarray) -> np.ndarray:
        op = depthcalc_op(blend_weight=self.args.midas_weight, export=True)
        results, _ = self.api.transform(self.prior_frames, [op])
        return results[0]

    def get_animation_prompts_weights(self, frame_idx: int) -> Tuple[List[str], List[float]]:
        keys = self.key_frame_values
        idx = bisect.bisect_right(keys, frame_idx)
        prev, next = idx - 1, idx
        if not self.args.interpolate_prompts:
            return [self.animation_prompts[keys[min(len(keys)-1, prev)]]], [1.0]
        elif next == len(keys):
            return [self.animation_prompts[keys[-1]]], [1.0]
        else:
            tween = (frame_idx - keys[prev]) / (keys[next] - keys[prev])
            return [self.animation_prompts[keys[prev]], self.animation_prompts[keys[next]]], [1.0 - tween, tween]

    def get_frame_filename(self, frame_idx, depth=False):
        prefix = "depth" if depth else "frame"
        return os.path.join(self.out_dir, f"{prefix}_{frame_idx:05d}.png")

    def save_settings(self, filename: str):
        settings_filepath = os.path.join(self.out_dir, filename)
        with open(settings_filepath, "w+", encoding="utf-8") as f:
            save_dict = args2dict(self.args)
            for k in ['angle', 'zoom', 'translation_x', 'translation_y', 'translation_z', 'rotation_x', 'rotation_y', 'rotation_z']:
                save_dict.move_to_end(k, last=True)
            save_dict['animation_prompts'] = self.animation_prompts
            save_dict['negative_prompt'] = self.negative_prompt
            save_dict['negative_prompt_weight'] = self.negative_prompt_weight
            json.dump(save_dict, f, ensure_ascii=False, indent=4)

    def setup_animation(self, resume):
        args = self.args

        # override generate endpoint to target user selected model
        self.api._generate.engine_id = args.model

        # change request for random seed into explicit value so it is saved to settings
        if args.seed <= 0:
            args.seed = random.randint(0, 2**32 - 1)

        def curve_to_series(curve: str) -> List[float]:
            return key_frame_inbetweens(key_frame_parse(curve), args.max_frames)    

        # expand key frame strings to per frame series
        self.frame_args = SimpleNamespace(**dict(
            angle_series = curve_to_series(args.angle),
            zoom_series = curve_to_series(args.zoom),
            translation_x_series = curve_to_series(args.translation_x),
            translation_y_series = curve_to_series(args.translation_y),
            translation_z_series = curve_to_series(args.translation_z),
            rotation_x_series = curve_to_series(args.rotation_x),
            rotation_y_series = curve_to_series(args.rotation_y),
            rotation_z_series = curve_to_series(args.rotation_z),
            brightness_series = curve_to_series(args.brightness_curve),
            contrast_series = curve_to_series(args.contrast_curve),
            noise_series = curve_to_series(args.noise_curve),
            noise_scale_series = curve_to_series(args.noise_scale_curve),
            steps_series = curve_to_series(args.steps_curve),
            strength_series = curve_to_series(args.strength_curve),
            diffusion_cadence_series = curve_to_series(args.diffusion_cadence_curve),
            fov_series = curve_to_series(args.fov_curve),
            video_mix_in_series = curve_to_series(args.video_mix_in_curve),
        ))

        # prepare sorted list of key frames
        self.key_frame_values = sorted(list(self.animation_prompts.keys()))
        if self.key_frame_values[0] != 0:
            raise ValueError("First keyframe must be 0")
        if len(self.key_frame_values) != len(set(self.key_frame_values)):
            raise ValueError("Duplicate keyframes are not allowed!")

        # initialize accumulated transforms
        self.prior_xforms = [matrix.identity, matrix.identity]

        # prepare inputs
        self.load_mask()
        self.load_video()
        self.load_init_image()

        # handle resuming animation from last frames of a previous run
        if resume:
            frames = [f for f in os.listdir(self.out_dir) if f.endswith(".png") and f.startswith("frame_")]
            self.start_frame_idx = len(frames)
            self.diffusion_cadence_ofs = self.start_frame_idx
            if self.start_frame_idx > 2:
                prev = cv2.imread(self.get_frame_filename(self.start_frame_idx-2))
                next = cv2.imread(self.get_frame_filename(self.start_frame_idx-1))
                self.prior_frames = [prev, next]
                self.prior_diffused = [prev, next]

    def load_init_image(self, fpath=None):
        if fpath is None:
            fpath =  self.args.init_image
        if not fpath:
            return
        img = cv2.imread(fpath)
        height, width, _ = img.shape
        if self.args.init_sizing == 'cover':
            scale = max(self.args.width / width, self.args.height / height)
            img = cv2.resize(img, (int(width * scale), int(height * scale)))
            x = (img.shape[1] - self.args.width) // 2
            y = (img.shape[0] - self.args.height) // 2
            img = img[y:y+self.args.height, x:x+self.args.width]
        elif self.args.init_sizing == 'stretch':
            img = cv2.resize(img, (self.args.width, self.args.height), interpolation=cv2.INTER_LANCZOS4)
        else: # 'resize-canvas'
            width, height = map(lambda x: x - x % 64, (width, height))
            self.args.width, self.args.height = width, height
            
        self.prior_frames = [img, img]
        self.prior_diffused = [img, img]

    def load_mask(self):
        if not self.args.mask_path:
            return

        # try to load mask as an image
        mask = cv2.imread(self.args.mask_path)
        if mask is not None:
            self.set_mask(mask)

        # try to load mask as a video
        if self.mask is None:
            self.mask_reader = cv2.VideoCapture(self.args.mask_path)
            self.next_mask()

        if self.mask is None:
            raise Exception(f"Failed to read mask from {self.args.mask_path}")

    def load_video(self):
        if self.args.animation_mode != 'Video Input' or not self.args.video_init_path:
            return

        self.video_reader = cv2.VideoCapture(self.args.video_init_path)
        if self.video_reader is not None:
            success, image = self.video_reader.read()
            if not success:
                raise Exception(f"Failed to read first frame from {self.args.video_init_path}")
            self.video_prev_frame = cv2.resize(image, (self.args.width, self.args.height), interpolation=cv2.INTER_LANCZOS4)
            self.prior_frames = [self.video_prev_frame, self.video_prev_frame]
            self.prior_diffused = [self.video_prev_frame, self.video_prev_frame]

    def next_mask(self):
        if not self.mask_reader:
            return False

        for _ in range(self.args.extract_nth_frame):
            success, mask = self.mask_reader.read()
            if not success:
                return

        self.set_mask(mask)

    def prepare_init(self, init_image: Optional[np.ndarray], frame_idx: int, noise_seed:int) -> Optional[np.ndarray]:
        if init_image is None:
            return None

        args, frame_args = self.args, self.frame_args
        noise = frame_args.noise_series[frame_idx]
        brightness = frame_args.brightness_series[frame_idx]
        contrast = frame_args.contrast_series[frame_idx]
        mix_in = frame_args.video_mix_in_series[frame_idx]

        init_ops = []
        if args.color_coherence != 'None' and self.color_match_image is not None:                    
            init_ops.append(colormatch_op(
                palette_image=self.color_match_image,
                color_mode=args.color_coherence,
            ))
        if mix_in > 0 and self.video_prev_frame is not None:
            init_ops.append(blend_op(
                amount=mix_in, 
                target=self.video_prev_frame
            ))
        if brightness != 1.0 or contrast != 1.0:
            init_ops.append(contrast_op(
                brightness=brightness,
                contrast=contrast,
            ))
        if noise > 0:
            init_ops.append(generation.TransformOperation(
                add_noise=generation.TransformAddNoise(amount=noise, seed=noise_seed)
            ))

        if len(init_ops):
            init_image = self.api.transform([init_image], init_ops)[0][0]

        return init_image

    def render(self) -> Generator[Image.Image, None, None]:
        args = self.args
        seed = args.seed

        for frame_idx in range(self.start_frame_idx, args.max_frames):
            diffusion_cadence = max(1, int(self.frame_args.diffusion_cadence_series[frame_idx]))
            steps = int(self.frame_args.steps_series[frame_idx])
            strength = max(0.0, self.frame_args.strength_series[frame_idx])
            adjusted_steps = int(max(5, steps*(1.0-strength))) if args.steps_strength_adj else int(steps)

            # fetch set of prompts and weights for this frame
            prompts, weights = self.get_animation_prompts_weights(frame_idx)
            if len(self.negative_prompt) and self.negative_prompt_weight != 0.0:
                prompts.append(self.negative_prompt)
                weights.append(-abs(self.negative_prompt_weight))

            # transform prior frames
            inpaint_mask = None
            stashed_prior_frames = [i.copy() for i in self.prior_frames] if self.mask is not None else None
            if args.animation_mode == '2D':
                inpaint_mask = self.transform_2d(frame_idx)
            elif args.animation_mode == '3D':
                inpaint_mask = self.transform_3d(frame_idx)
            elif args.animation_mode == 'Video Input':
                inpaint_mask = self.transform_video(frame_idx)

            # apply inpainting
            if args.inpaint_border and inpaint_mask is not None:
                self.apply_inpainting(inpaint_mask, prompts, weights, adjusted_steps, seed)

            # update diffusion mask
            self.next_mask()

            # apply mask to transformed prior frames
            if self.mask is not None:
                for i in range(len(self.prior_frames)):
                    self.prior_frames[i] = image_mix(self.prior_frames[i], stashed_prior_frames[i], self.mask)

            # either run diffusion or emit an inbetween frame
            if (frame_idx - self.diffusion_cadence_ofs) % diffusion_cadence == 0:
                # apply additional noising and color matching to previous frame to use as init
                init_image = self.prior_frames[-1] if len(self.prior_frames) and strength > 0 else None
                init_image = self.prepare_init(init_image, frame_idx, seed)

                # generate the next frame
                sampler = sampler_from_string(args.sampler.lower())
                guidance = guidance_from_string(args.clip_guidance)
                noise_scale = self.frame_args.noise_scale_series[frame_idx]
                image = self.api.generate(
                    prompts, weights, 
                    args.width, args.height, 
                    steps=adjusted_steps,
                    seed=seed,
                    cfg_scale=args.cfg_scale,
                    sampler=sampler, 
                    init_image=init_image, 
                    init_strength=strength,
                    init_noise_scale=noise_scale, 
                    mask = self.mask,
                    masked_area_init=generation.MASKED_AREA_INIT_ORIGINAL,
                    guidance_preset=guidance,
                )

                if self.color_match_image is None and args.color_coherence != 'None':
                    self.color_match_image = image
                if not len(self.prior_frames):
                    self.prior_frames = [image, image]
                    self.prior_diffused = [image, image]
                    self.prior_xforms = [matrix.identity, matrix.identity]

                self.prior_diffused = [self.prior_diffused[1], image]
                self.prior_frames = [self.prior_frames[1], image]
                self.prior_xforms = [self.prior_xforms[1], matrix.identity]
                self.diffusion_cadence_ofs = frame_idx
                out_frame = image if diffusion_cadence == 1 else self.prior_frames[0]
            else:
                # smoothly blend between prior frames
                tween = ((frame_idx - self.diffusion_cadence_ofs) % diffusion_cadence) / float(diffusion_cadence)
                out_frame = self.api.interpolate(
                    [self.prior_frames[0], self.prior_frames[1]],
                    [tween],
                    interp_mode_from_str(args.cadence_interp)
                )[0]

            cv2.imwrite(self.get_frame_filename(frame_idx), out_frame)
            yield cv2_to_pil(out_frame)
            if args.save_depth_maps:
                depth_image = self.generate_depth_image(out_frame)
                cv2.imwrite(self.get_frame_filename(frame_idx, depth=True), depth_image)

            if not args.locked_seed:
                seed += 1

    def set_mask(self, mask: np.ndarray):
        self.mask = cv2.resize(mask, (self.args.width, self.args.height), interpolation=cv2.INTER_LANCZOS4)
        self.mask = cv2.cvtColor(self.mask, cv2.COLOR_BGR2GRAY)

        # this is intentionally flipped because we want white in the mask to represent
        # areas that should change which is opposite from the backend which treats
        # the mask as per pixel offset in the schedule starting value
        if not self.args.mask_invert:
            self.mask = 255 - self.mask

    def transform_2d(self, frame_idx) -> Optional[np.ndarray]:
        if not len(self.prior_frames):
            return None

        args, frame_args = self.args, self.frame_args
        angle = frame_args.angle_series[frame_idx]
        scale = frame_args.zoom_series[frame_idx]
        dx = frame_args.translation_x_series[frame_idx]
        dy = frame_args.translation_y_series[frame_idx]

        if angle == 0.0 and scale == 1.0 and dx == 0.0 and dy == 0.0:
            return None

        if args.accumulate_xforms:
            frame_args = self.frame_args

            # create xform for the current frame
            xform = make_xform_2dm(args.width, args.height, math.radians(angle), scale, dx, dy)

            # apply xform to prior frames running xforms
            for i in range(len(self.prior_xforms)):
                self.prior_xforms[i] = matrix.multiply(xform, self.prior_xforms[i])

            # warp prior diffused frames by accumulated xforms
            for i in range(len(self.prior_diffused)):
                matrix_flat = sum(to_3x3(self.prior_xforms[i]), []) 
                op = generation.TransformOperation(
                    warp2d=generation.TransformWarp2d(
                        border_mode = border_mode_from_str_2d(args.border),
                        matrix=generation.TransformMatrix(data=matrix_flat))
                )                
                xformed, _ = self.api.transform([self.prior_diffused[i]], [op])
                self.prior_frames[i] = xformed[0]

            return None
        else:
            op = warp2d_op(
                border_mode=args.border,
                rotate=angle, scale=scale,
                translate_x=dx, translate_y=dy,
            )
            self.prior_frames, mask = self.api.transform(self.prior_frames, [op])
            return mask

    def transform_3d(self, frame_idx) -> Optional[np.ndarray]:
        if not len(self.prior_frames):
            return None

        args, frame_args = self.args, self.frame_args

        dx = frame_args.translation_x_series[frame_idx]
        dy = frame_args.translation_y_series[frame_idx]
        dz = frame_args.translation_z_series[frame_idx]
        rx = frame_args.rotation_x_series[frame_idx]
        ry = frame_args.rotation_y_series[frame_idx]
        rz = frame_args.rotation_z_series[frame_idx]
        near, far = args.near_plane, args.far_plane
        fov = frame_args.fov_series[frame_idx]

        if args.accumulate_xforms:
            TRANSLATION_SCALE = 1.0/200.0 # matches Disco
            dx, dy, dz = -dx*TRANSLATION_SCALE, dy*TRANSLATION_SCALE, -dz*TRANSLATION_SCALE
            rx, ry, rz = math.radians(rx), math.radians(ry), math.radians(rz)

            # create xform for the current frame
            world_view = matrix.multiply(matrix.translation(dx, dy, dz), matrix.rotation_euler(rx, ry, rz))
            projection = matrix.projection_fov(math.radians(fov), 1.0, near, far)           

            # apply world_view xform to prior frames running xforms
            for i in range(len(self.prior_xforms)):
                self.prior_xforms[i] = matrix.multiply(world_view, self.prior_xforms[i])

            # warp prior diffused frames by accumulated xforms
            for i in range(len(self.prior_diffused)):
                wvp = matrix.multiply(projection, self.prior_xforms[i])
                ops = [
                    generation.TransformOperation(depth_calc=generation.TransformDepthCalc(
                        blend_weight=args.midas_weight,
                        export=False,
                        blur_radius=5
                    )),
                    generation.TransformOperation(
                        resample=generation.TransformResample(
                            border_mode=border_mode_from_str_3d(args.border),
                            transform=generation.TransformMatrix(data=sum(wvp, [])),
                            prev_transform=generation.TransformMatrix(data=sum(projection, [])),
                            depth_warp=1.0,
                            export_mask=False,
                        )
                    ),
                ]
                xformed, mask = self.api.transform([self.prior_diffused[i]], ops)
                self.prior_frames[i] = xformed[0]

            return mask
        else:
            ops = [
                depthcalc_op(blend_weight=args.midas_weight),
                warp3d_op(
                    border_mode = args.border,
                    translate_x = dx, translate_y = dy, translate_z = dz,
                    rotate_x = rx, rotate_y = ry, rotate_z = rz,
                    near_plane = near, far_plane = far, fov = fov
                )
            ]
            self.prior_frames, mask = self.api.transform(self.prior_frames, ops)
            return mask

    def transform_video(self, frame_idx) -> Optional[np.ndarray]:
        if not len(self.prior_frames):
            return None

        op = None
        args = self.args
        for _ in range(args.extract_nth_frame):
            success, video_next_frame = self.video_reader.read()
        if success:
            video_next_frame = cv2.resize(
                video_next_frame,
                (args.width, args.height),
                interpolation=cv2.INTER_LANCZOS4
            )
            if args.video_flow_warp:
                op = warpflow_op(
                    prev_frame=self.video_prev_frame,
                    next_frame=video_next_frame,
                )
            self.video_prev_frame = video_next_frame
            self.color_match_image = video_next_frame

            self.prior_frames, mask = self.api.transform(self.prior_frames, [op])
            return mask
        return None

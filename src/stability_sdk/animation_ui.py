import glob
import gradio as gr
import json
import locale
import os
import param

from collections import OrderedDict
from tqdm import tqdm
from typing import Any, Dict, List, Optional

from .api import (
    ClassifierException, 
    Context,
    Project,
    OutOfCreditsException,
)
from .animation import (
    AnimationArgs,
    Animator,
    AnimationSettings,
    BasicSettings,
    CameraSettings,
    CoherenceSettings,
    ColorSettings,
    DepthSettings,
    InpaintingSettings,
    Rendering3dSettings,
    VideoInputSettings,
    VideoOutputSettings,
    create_video_from_frames,
    interpolate_frames
)
from .utils import interp_mode_from_str


DATA_VERSION = "0.1"
DATA_GENERATOR = "alpha-test-notebook"

PRESETS = {
    "Default": {},
    "3D warp rotate": {"animation_mode": "3D warp", "rotation_y":"0:(0.4)", "translation_x":"0:(-1.2)"},
    "3D warp zoom": {
        "animation_mode":"3D warp", "diffusion_cadence_curve":"0:(4)", "noise_scale_curve":"0:(1.04)", 
        "strength_curve":"0:(0.7)", "translation_z":"0:(1.0)",
    },
    "3D render rotate": {
        "animation_mode": "3D render", "translation_x":"0:(-2)", "rotation_y":"0:(-0.8)",
        "diffusion_cadence_curve":"0:(1)", "strength_curve":"0:(0.98)",
        "noise_scale_curve":"0:(1.01)", "depth_model_weight":0.3,
        "mask_min_value":"0:(0.45)", "non_inpainting_model_for_diffusion_frames":True,
    },
    "3D render explore": {
        "animation_mode": "3D render", "translation_z":"0:(10)", "translation_x":"0:(2), 20:(-2), 40:(2)",
        "rotation_y":"0:(0), 10:(1.5), 30:(-2), 50: (3)", "rotation_x":"0:(0.4)",
        "diffusion_cadence_curve":"0:(1)", "strength_curve":"0:(0.98)",
        "noise_scale_curve":"0:(1.01)", "depth_model_weight":0.3,
        "mask_min_value":"0:(0.1)", "non_inpainting_model_for_diffusion_frames":True,
    },
    "Prompt interpolate": {
        "animation_mode":"2D", "interpolate_prompts":True, "locked_seed":True, "max_frames":48, 
        "strength_curve":"0:(0)", "diffusion_cadence_curve":"0:(4)", "cadence_interp":"film",
        "clip_guidance":"None", "animation_prompts": "{\n0:\"a photo of a cute cat\",\n24:\"a photo of a cute dog\"\n}"
    },
    "Outpaint": {
        "animation_mode":"2D", "diffusion_cadence_curve":"0:(24)", "cadence_spans":True, "strength_curve":"0:(0.75)",
        "inpaint_border":True, "zoom":"0:(0.95)", "animation_prompts": "{\n0:\"an ancient and magical portal, in a fantasy corridor\"\n}"
    },
    "Video Stylize": {
        "animation_mode":"Video Input", "model":"stable-diffusion-depth-v2-0", "locked_seed":True, 
        "strength_curve":"0:(0.22)", "clip_guidance":"None", "video_mix_in_curve":"0:(1.0)", "video_flow_warp":True,
    },
}

context = None
outputs_path = None

args_generation = BasicSettings()
args_animation = AnimationSettings()
args_camera = CameraSettings()
args_coherence = CoherenceSettings()
args_color = ColorSettings()
args_depth = DepthSettings()
args_render_3d = Rendering3dSettings()
args_inpaint = InpaintingSettings()
args_vid_in = VideoInputSettings()
args_vid_out = VideoOutputSettings()
arg_objs = (
    args_generation,
    args_animation,
    args_camera,
    args_coherence,
    args_color,
    args_depth,
    args_render_3d,
    args_inpaint,
    args_vid_in,
    args_vid_out,
)

animation_prompts = "{\n0: \"\"\n}"
negative_prompt = "blurry, low resolution"
negative_prompt_weight = -1.0

controls: Dict[str, gr.components.Component] = {}
header = gr.HTML("", show_progress=False)
interrupt = False
last_project_settings_path = None
last_interp_factor = None
last_interp_mode = None
projects: List[Project] = []
project: Optional[Project] = None

project_create_button = gr.Button("Create")
project_data_log = gr.Textbox(label="Status", visible=False)
project_load_button = gr.Button("Load")
project_new_title = gr.Text(label="Name", value="My amazing animation", interactive=True)
project_preset_dropdown = gr.Dropdown(label="Preset", choices=list(PRESETS.keys()), value=list(PRESETS.keys())[0], interactive=True)
project_row_create = None
project_row_import = None
project_row_load = None
projects_dropdown = gr.Dropdown([p.title for p in projects], label="Project", visible=True, interactive=True)

project_import_button = gr.Button("Import")
project_import_file = gr.File(label="Project file", file_types=[".json", ".txt"], type="binary")
project_import_title = gr.Text(label="Name", value="Imported project", interactive=True)


def accordion_for_color(args: ColorSettings):
    p = args.param
    with gr.Accordion("Color", open=False):
        controls["color_coherence"] = gr.Dropdown(label="Color coherence", choices=p.color_coherence.objects, value=p.color_coherence.default, interactive=True)
        with gr.Row():
            controls["brightness_curve"] = gr.Text(label="Brightness curve", value=p.brightness_curve.default, interactive=True)
            controls["contrast_curve"] = gr.Text(label="Contrast curve", value=p.contrast_curve.default, interactive=True)
        with gr.Row():
            controls["hue_curve"] = gr.Text(label="Hue curve", value=p.hue_curve.default, interactive=True)
            controls["saturation_curve"] = gr.Text(label="Saturation curve", value=p.saturation_curve.default, interactive=True)
            controls["lightness_curve"] = gr.Text(label="Lightness curve", value=p.lightness_curve.default, interactive=True)

def accordion_from_args(name: str, args: param.Parameterized, exclude: List[str]=[], open=False):
    with gr.Accordion(name, open=open):
        ui_from_args(args, exclude)

def args_reset_to_defaults():
    for args in arg_objs:
        for k, v in args.param.objects().items():
            if k == "name":
                continue
            setattr(args, k, v.default)

def args_to_controls(data: Optional[dict]=None) -> dict:    
    # go through all the parameters and load their settings from the data
    global animation_prompts, negative_prompt
    if data:
        for arg in arg_objs:
            for k, v in arg.param.objects().items():
                if k != "name" and k in data:
                    arg.param.set_param(k, data[k])
        if "animation_prompts" in data:
            animation_prompts = data["animation_prompts"]
        if "negative_prompt" in data:
            negative_prompt = data["negative_prompt"]

    returns = {}
    returns[controls['animation_prompts']] = gr.update(value=animation_prompts)
    returns[controls['negative_prompt']] = gr.update(value=negative_prompt)

    for args in arg_objs:
        for k, v in args.param.objects().items():
            if k in controls:
                c = controls[k]
                returns[c] = gr.update(value=getattr(args, k))

    return returns

def ensure_api_context():
    if context is None:
        raise gr.Error("Not connected to Stability API")

def format_header_html() -> str:
    balance, profile_picture = context.get_user_info()
    formatted_number = locale.format_string("%d", balance, grouping=True)
    return f"""
        <div class="flex flex-row items-center" style="display:flex; justify-content: space-between; margin-top: 8px;">
            <div>StabilityAI Stable Diffusion Animation</div>
            <div class="flex cursor-pointer flex-row items-center gap-1" style="display:flex; gap: 0.25rem; justify-content: flex-end;">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="h-4 w-4">
                    <circle cx="8" cy="8" r="6"></circle>
                    <path d="M18.09 10.37A6 6 0 1 1 10.34 18"></path>
                    <path d="M7 6h1v4"></path>
                    <path d="m16.71 13.88.7.71-2.82 2.82"></path>
                </svg>
                {formatted_number}
                <div style="width:28px; height:28px; overflow:hidden; border-radius:50%;">
                    <img alt="user avatar" src="{profile_picture}" class="MuiAvatar-img css-1hy9t21">
                </div>
            </div>
        </div>
    """

def get_default_project():
    data = {
        "version": DATA_VERSION,
        "generator": DATA_GENERATOR
    }
    return data

def post_process_tab():
    with gr.Row():
        with gr.Column():
            fps = gr.Number(label="FPS", value=24, interactive=True, precision=0)
            reverse = gr.Checkbox(label="Reverse", value=False, interactive=True)
            with gr.Row():
                frame_interp_mode = gr.Dropdown(label="Frame interpolation mode", choices=['None', 'film', 'rife'], value='None', interactive=True)       
                frame_interp_factor = gr.Dropdown(label="Frame interpolation factor", choices=[2, 4, 8], value=2, interactive=True)
        with gr.Column():
            image_out = gr.Image(label="image", visible=True)
            video_out = gr.Video(label="video", visible=False)
            process_button = gr.Button("Process")
            stop_button = gr.Button("Stop", visible=False)

    def postprocess_video(fps: int, reverse: bool, interp_mode: str, interp_factor: int):
        global interrupt, last_interp_factor, last_interp_mode
        interrupt = False
        if last_project_settings_path is None:
            raise gr.Error("Must render an animation first")

        yield {
            header: gr.update(),
            image_out: gr.update(visible=True, label=""),
            video_out: gr.update(visible=False),
            process_button: gr.update(visible=False),
            stop_button: gr.update(visible=True),
        }

        outdir = os.path.dirname(last_project_settings_path)

        if interp_mode != 'None':
            interp_dir = os.path.join(outdir, "post")
            interp_mode = interp_mode_from_str(interp_mode)
            if last_interp_mode != interp_mode or last_interp_factor != interp_factor:                
                remove_frames_from_path(interp_dir)
                num_frames = interp_factor * len(glob.glob(os.path.join(outdir, "frame_*.png")))
                for frame_idx, frame in enumerate(tqdm(interpolate_frames(context, outdir, interp_dir, interp_mode, interp_factor), total=num_frames)):
                    yield {
                        header: gr.update(value=format_header_html()) if frame_idx % 12 == 0 else gr.update(),
                        image_out: gr.update(value=frame, label=f"frame {frame_idx}/{num_frames}", visible=True),
                        video_out: gr.update(visible=False),
                        process_button: gr.update(visible=False),
                        stop_button: gr.update(visible=True),
                    }
                    if interrupt:
                        break
                last_interp_mode, last_interp_factor = interp_mode, interp_factor
            outdir = interp_dir

        output_video = last_project_settings_path.replace(".json", ".mp4")
        create_video_from_frames(outdir, output_video, fps=fps, reverse=reverse)
        yield {
            header: gr.update(value=format_header_html()),
            image_out: gr.update(visible=False),
            video_out: gr.update(value=output_video, visible=True),
            process_button: gr.update(visible=True),
            stop_button: gr.update(visible=False),
        }

    process_button.click(
        postprocess_video, 
        inputs=[fps, reverse, frame_interp_mode, frame_interp_factor], 
        outputs=[header, image_out, video_out, process_button, stop_button]
    )    

    def stop_button_click():
        global interrupt
        interrupt = True
    stop_button.click(stop_button_click)


def project_create(title, preset):
    ensure_api_context()
    global project, projects
    titles = [p.title for p in projects]
    if title in titles:
        raise gr.Error(f"Project with title '{title}' already exists")
    project = Project.create(context, title)
    settings = get_default_project()

    # grab each setting from the preset and add to settings
    for k, v in PRESETS[preset].items():
        settings[k] = v

    project.save_settings(settings)
    projects = Project.list_projects(context)
    log = f"Created project '{title}' with id {project.id}\n{json.dumps(settings)}"

    args_reset_to_defaults()
    returns = args_to_controls(settings)
    returns[project_data_log] = gr.update(value=log, visible=True)
    returns[projects_dropdown] = gr.update(choices=[p.title for p in projects], visible=True, value=title)
    returns[project_row_load] = gr.update(visible=len(projects) > 0)
    return returns

def project_import(title, file):
    ensure_api_context()
    global project, projects
    titles = [p.title for p in projects]
    if title in titles:
        raise gr.Error(f"Project with title '{title}' already exists")
    project = Project.create(context, title)

    # read json from file
    try:
        settings = json.loads(file.decode('utf-8'))
    except Exception as e:
        raise gr.Error(f"Failed to read settings from file: {e}")

    project.save_settings(settings)
    projects = Project.list_projects(context)
    log = f"Created project '{title}' with id {project.id}\n{json.dumps(settings)}"

    args_reset_to_defaults()
    returns = args_to_controls(settings)
    returns[project_data_log] = gr.update(value=log, visible=True)
    returns[projects_dropdown] = gr.update(choices=[p.title for p in projects], visible=True, value=title)
    returns[project_row_load] = gr.update(visible=len(projects) > 0)
    return returns

def project_load(title: str):
    ensure_api_context()
    global project
    project = next(p for p in projects if p.title == title)
    try:
        data = project.load_settings()
    except OutOfCreditsException as e:
        log = f"Not enough credits to load project '{title}'\n{e.details}"
        returns = args_to_controls(get_default_project())
        returns[project_data_log] = gr.update(value=log, visible=True)
        return returns

    log = f"Loaded project '{title}' with id {project.id}\n{json.dumps(data, indent=4)}"

    # filter project file to latest version
    if "animation_mode" in data and data["animation_mode"] == "3D":
        data["animation_mode"] = "3D warp"
    if "midas_weight" in data:
        data["depth_model_weight"] = data["midas_weight"]
        del data["midas_weight"]

    # update the ui controls
    returns = args_to_controls(data)
    returns[project_data_log] = gr.update(value=log, visible=True)
    return returns

def project_tab():
    global project_row_create, project_row_import, project_row_load

    button_load_projects = gr.Button("Load Projects")
    with gr.Accordion("Load a project", open=True, visible=False) as projects_row_:
        project_row_load = projects_row_
        with gr.Row():
            projects_dropdown.render()
            with gr.Column():
                project_load_button.render()
                button_delete_project = gr.Button("Delete")

    with gr.Accordion("Create a new project", open=True, visible=False) as project_row_create_:
        project_row_create = project_row_create_
        with gr.Row():
            with gr.Row():
                project_new_title.render()
                project_preset_dropdown.render()
            project_create_button.render()

    with gr.Accordion("Import a project file", open=False, visible=False) as project_row_import_:
        project_row_import = project_row_import_
        with gr.Row():
            with gr.Column():
                project_import_file.render()
            with gr.Column():
                project_import_title.render()
                project_import_button.render()

    project_data_log.render()

    def delete_project(title: str):
        ensure_api_context()
        global project, projects
        project = next(p for p in projects if p.title == title)
        project.delete()
        log = f"Deleted project '{title}' with id {project.id}"
        projects = Project.list_projects(context)
        project = None
        return {
            projects_dropdown: gr.update(choices=[p.title for p in projects], visible=True),
            project_row_load: gr.update(visible=len(projects) > 0),
            project_data_log: gr.update(value=log, visible=True)
        }

    def load_projects():
        ensure_api_context()
        global projects
        projects = Project.list_projects(context)
        return {
            button_load_projects: gr.update(visible=len(projects)==0),
            projects_dropdown: gr.update(choices=[p.title for p in projects], visible=True),
            project_row_create: gr.update(visible=True),
            project_row_import: gr.update(visible=True),
            project_row_load: gr.update(visible=len(projects) > 0),
            header: gr.update(value=format_header_html())
        }

    button_load_projects.click(load_projects, outputs=[button_load_projects, projects_dropdown, project_row_create, project_row_import, project_row_load, header])
    button_delete_project.click(delete_project, inputs=projects_dropdown, outputs=[projects_dropdown, project_row_load, project_data_log])

def remove_frames_from_path(path):
    if os.path.isdir(path):
        for f in glob.glob(os.path.join(path, "frame_*.png")):
            os.remove(f)

def render_tab():
    with gr.Row():
        with gr.Column():
            ui_layout_tabs()
        with gr.Column():
            image_out = gr.Image(label="image", visible=True)
            video_out = gr.Video(label="video", visible=False)
            button = gr.Button("Render")
            button_stop = gr.Button("Stop", visible=False)
            error_log = gr.Textbox(label="Error", lines=3, visible=False)

    def render(*render_args):
        global interrupt, last_interp_factor, last_interp_mode, last_project_settings_path, project
        interrupt = False

        if not project:
            raise gr.Error("No project active!")
        
        # create local folder for the project
        project_folder_name = project.title.replace("/", "_").replace("\\", "_").replace(":", "")
        outdir = os.path.join(outputs_path, project_folder_name)
        os.makedirs(outdir, exist_ok=True)

        # each render gets a unique run index
        run_index = 0
        while True:
            project_settings_path = os.path.join(outdir, f"{project_folder_name} ({run_index}).json")
            if not os.path.exists(project_settings_path):
                break
            run_index += 1

        # gather up all the settings from sub-objects
        args_d = {k: v for k, v in zip(controls.keys(), render_args)}
        animation_prompts, negative_prompt = args_d['animation_prompts'], args_d['negative_prompt']
        del args_d['animation_prompts'], args_d['negative_prompt']
        args = AnimationArgs(**args_d)

        if args.animation_mode == "Video Input" and not args.video_init_path:
            raise gr.Error("No video input file selected!")

        # convert animation_prompts from string (JSON or python) to dict
        try:
            prompts = json.loads(animation_prompts)
        except json.JSONDecodeError:
            try:
                prompts = eval(animation_prompts)
            except Exception as e:
                raise gr.Error("Invalid JSON or Python code for animation_prompts!")
        prompts = {int(k): v for k, v in prompts.items()}

        # save settings to a dict
        save_dict = OrderedDict()
        save_dict['version'] = DATA_VERSION
        save_dict['generator'] = DATA_GENERATOR
        save_dict.update(args.param.values())
        save_dict['animation_prompts'] = animation_prompts
        save_dict['negative_prompt'] = negative_prompt
        try:
            project.save_settings(save_dict)
        except OutOfCreditsException as e:
            raise gr.Error(e.details)
        with open(project_settings_path, 'w', encoding='utf-8') as f:
            json.dump(save_dict, f, indent=4)

        # initial yield to switch render button to stop button
        yield {
            button: gr.update(visible=False),
            button_stop: gr.update(visible=True),
            image_out: gr.update(visible=True, label=""),
            video_out: gr.update(visible=False),
            header: gr.update(),
            error_log: gr.update(visible=False),
        }

        # delete frames from previous animation
        remove_frames_from_path(outdir)

        frame_idx, error = 0, None
        try:
            animator = Animator(
                api_context=context,
                animation_prompts=prompts,
                args=args,
                out_dir=outdir,
                negative_prompt=negative_prompt,
                negative_prompt_weight=negative_prompt_weight,
                resume=False,
            )
            for frame_idx, frame in enumerate(tqdm(animator.render(), initial=animator.start_frame_idx, total=args.max_frames)):
                if interrupt:
                    break

                # saving frames to project
                #frame_uuid = project.put_image_asset(frame)

                yield {
                    button: gr.update(visible=False),
                    button_stop: gr.update(visible=True),
                    image_out: gr.update(value=frame, label=f"frame {frame_idx}/{args.max_frames}", visible=True),
                    video_out: gr.update(visible=False),
                    header: gr.update(value=format_header_html()) if frame_idx % 12 == 0 else gr.update(),
                    error_log: gr.update(visible=False),
                }
        except ClassifierException as e:
            error = "Animation terminated early due to NSFW classifier."
            if e.prompt is not None:
                error += "\nPlease revise your prompt: " + e.prompt
        except OutOfCreditsException as e:
            error = f"Animation terminated early, out of credits.\n{e.details}"
        except Exception as e:
            error = f"Animation terminated early due to exception: {e}"

        if frame_idx:
            last_project_settings_path = project_settings_path
            last_interp_factor, last_interp_mode = None, None
            output_video = project_settings_path.replace(".json", ".mp4")
            create_video_from_frames(outdir, output_video, fps=args.fps, reverse=args.reverse)
        else:
            output_video = None
        yield {
            button: gr.update(visible=True),
            button_stop: gr.update(visible=False),
            image_out: gr.update(visible=False),
            video_out: gr.update(value=output_video, visible=True),
            header: gr.update(value=format_header_html()),
            error_log: gr.update(value=error, visible=bool(error)),
        }

    button.click(
        render,
        inputs=list(controls.values()),
        outputs=[button, button_stop, image_out, video_out, header, error_log]
    )

    # stop animation in progress 
    def stop():
        global interrupt
        interrupt = True
    button_stop.click(stop)

def ui_for_animation_settings(args: AnimationSettings):
    with gr.Row():
        controls["steps_strength_adj"] = gr.Checkbox(label="Steps strength adj", value=args.param.steps_strength_adj.default, interactive=True)
        controls["interpolate_prompts"] = gr.Checkbox(label="Interpolate prompts", value=args.param.interpolate_prompts.default, interactive=True)
        controls["locked_seed"] = gr.Checkbox(label="Locked seed", value=args.param.locked_seed.default, interactive=True)
    controls["noise_add_curve"] = gr.Text(label="Noise add curve", value=args.param.noise_add_curve.default, interactive=True)
    controls["noise_scale_curve"] = gr.Text(label="Noise scale curve", value=args.param.noise_scale_curve.default, interactive=True)
    controls["strength_curve"] = gr.Text(label="Previous frame strength curve", value=args.param.strength_curve.default, interactive=True)
    controls["steps_curve"] = gr.Text(label="Steps curve", value=args.param.steps_curve.default, interactive=True)

def ui_for_generation(args: AnimationSettings):
    p = args.param
    with gr.Row():
        controls["width"] = gr.Number(label="Width", value=p.width.default, interactive=True, precision=0)
        controls["height"] = gr.Number(label="Height", value=p.height.default, interactive=True, precision=0)
    with gr.Row():
        controls["model"] = gr.Dropdown(label="Model", choices=p.model.objects, value=p.model.default, interactive=True)
        controls["custom_model"] = gr.Text(label="Custom model", value=p.custom_model.default, interactive=True)
    with gr.Row():
        controls["preset"] = gr.Dropdown(label="Style preset", choices=p.preset.objects, value=p.preset.default, interactive=True)
    with gr.Row():
        controls["sampler"] = gr.Dropdown(label="Sampler", choices=p.sampler.objects, value=p.sampler.default, interactive=True)
        controls["seed"] = gr.Number(label="Seed", value=p.seed.default, interactive=True, precision=0)
        controls["cfg_scale"] = gr.Number(label="Guidance scale", value=p.cfg_scale.default, interactive=True)
        controls["clip_guidance"] = gr.Dropdown(label="CLIP guidance", choices=p.clip_guidance.objects, value=p.clip_guidance.default, interactive=True)

def ui_for_init_and_mask(args_generation):
    p = args_generation.param
    with gr.Row():
        controls["init_image"] = gr.Text(label="Init image", value=p.init_image.default, interactive=True)
        controls["init_sizing"] = gr.Dropdown(label="Init sizing", choices=p.init_sizing.objects, value=p.init_sizing.default, interactive=True)
    with gr.Row():
        controls["mask_path"] = gr.Text(label="Mask path", value=p.mask_path.default, interactive=True)
        controls["mask_invert"] = gr.Checkbox(label="Mask invert", value=p.mask_invert.default, interactive=True)

def ui_for_video_output(args: VideoOutputSettings):
    p = args.param
    controls["fps"] = gr.Number(label="FPS", value=p.fps.default, interactive=True, precision=0)
    controls["reverse"] = gr.Checkbox(label="Reverse", value=p.reverse.default, interactive=True)
    with gr.Row():
        controls["vr_mode"] = gr.Checkbox(label="VR Mode", value=p.vr_mode.default, interactive=True)
        controls["vr_eye_angle"] = gr.Number(label="Eye angle", value=p.vr_eye_angle.default, interactive=True)
        controls["vr_eye_dist"] = gr.Number(label="Eye distance", value=p.vr_eye_dist.default, interactive=True)
        controls["vr_projection"] = gr.Number(label="Spherical projection", value=p.vr_projection.default, interactive=True)

def ui_from_args(args: param.Parameterized, exclude: List[str]=[]):
    for k, v in args.param.objects().items():
        if k == "name" or k in exclude:
            continue
        elif isinstance(v, param.Integer):
            t = gr.Number(label=v.label, value=v.default, interactive=True, precision=0)
        elif isinstance(v, param.ObjectSelector):
            t = gr.Dropdown(label=v.label, choices=v.objects, value=v.default, interactive=True)
        elif isinstance(v, param.Boolean):
            t = gr.Checkbox(label=v.label, value=v.default, interactive=True)
        elif isinstance(v, param.String):
            t = gr.Text(label=v.label, value=v.default, interactive=True)
        elif isinstance(v, param.Number):
            t = gr.Number(label=v.label, value=v.default, interactive=True)
        controls[k] = t

def ui_layout_tabs():
    with gr.Tab("Prompts"):
        with gr.Row():
            controls['animation_prompts'] = gr.TextArea(label="Animation prompts", max_lines=8, value=animation_prompts, interactive=True)
        with gr.Row():
            controls['negative_prompt'] = gr.Textbox(label="Negative prompt", max_lines=1, value=negative_prompt, interactive=True)
    with gr.Tab("Config"):
        with gr.Row():
            args = args_animation
            controls["animation_mode"] = gr.Dropdown(label="Animation mode", choices=args.param.animation_mode.objects, value=args.param.animation_mode.default, interactive=True)
            controls["max_frames"] = gr.Number(label="Max frames", value=args.param.max_frames.default, interactive=True, precision=0)
            controls["border"] = gr.Dropdown(label="Border", choices=args.param.border.objects, value=args.param.border.default, interactive=True)
        ui_for_generation(args_generation)
        ui_for_animation_settings(args_animation)
        accordion_from_args("Coherence", args_coherence, open=False)
        accordion_for_color(args_color)
        accordion_from_args("Depth", args_depth, exclude=["near_plane", "far_plane"], open=False)
        accordion_from_args("3D render", args_render_3d, open=False)
        accordion_from_args("Inpainting", args_inpaint, open=False)
    with gr.Tab("Input"):
        ui_for_init_and_mask(args_generation)
        with gr.Column():
            p = args_vid_in.param
            with gr.Row():
                controls["video_init_path"] = gr.Text(label="Video init path", value=p.video_init_path.default, interactive=True)
            with gr.Row():
                controls["video_mix_in_curve"] = gr.Text(label="Mix in curve", value=p.video_mix_in_curve.default, interactive=True)
                controls["extract_nth_frame"] = gr.Number(label="Extract nth frame", value=p.extract_nth_frame.default, interactive=True, precision=0)
                controls["video_flow_warp"] = gr.Checkbox(label="Flow warp", value=p.video_flow_warp.default, interactive=True)

    with gr.Tab("Camera"):
        p = args_camera.param
        gr.Markdown("2D Camera")
        controls["angle"] = gr.Text(label="Angle", value=p.angle.default, interactive=True)
        controls["zoom"] = gr.Text(label="Zoom", value=p.zoom.default, interactive=True)

        gr.Markdown("2D and 3D Camera translation")
        controls["translation_x"] = gr.Text(label="Translation X", value=p.translation_x.default, interactive=True)
        controls["translation_y"] = gr.Text(label="Translation Y", value=p.translation_y.default, interactive=True)
        controls["translation_z"] = gr.Text(label="Translation Z", value=p.translation_z.default, interactive=True)

        gr.Markdown("3D Camera rotation")
        controls["rotation_x"] = gr.Text(label="Rotation X", value=p.rotation_x.default, interactive=True)
        controls["rotation_y"] = gr.Text(label="Rotation Y", value=p.rotation_y.default, interactive=True)
        controls["rotation_z"] = gr.Text(label="Rotation Z", value=p.rotation_z.default, interactive=True)

    with gr.Tab("Output"):
        ui_for_video_output(args_vid_out)


def create_ui(api_context: Context, outputs_root_path: str):
    global context, outputs_path, projects
    context, outputs_path = api_context, outputs_root_path

    locale.setlocale(locale.LC_ALL, '')

    with gr.Blocks() as ui:
        header.render()

        with gr.Tab("Project"):
            project_tab()

        with gr.Tab("Render"):
            render_tab()

        with gr.Tab("Post-process"):
            post_process_tab()

        load_project_outputs = [project_data_log]
        load_project_outputs.extend(controls.values())
        project_load_button.click(project_load, inputs=projects_dropdown, outputs=load_project_outputs)

        create_project_outputs = [project_data_log, projects_dropdown, project_row_load]
        create_project_outputs.extend(controls.values())
        project_create_button.click(project_create, inputs=[project_new_title, project_preset_dropdown], outputs=create_project_outputs)
        project_import_button.click(project_import, inputs=[project_import_title, project_import_file], outputs=create_project_outputs)

    return ui

import logging
from dataclasses import dataclass, fields
import argparse

from .palettes import PALETTES
from .constants import UPSCALING_METHODS

logger = logging.getLogger(__name__)


@dataclass
class DaemonConfig:
    dev_video_file: str = "/dev/video20"
    show_min_max_temp: bool = False
    show_center_temp: bool = False
    show_colorbar: bool = False
    palette: str = "Inferno"
    upscaling_method: str = UPSCALING_METHODS[0]
    rotate_deg: int = 0
    flip: bool = False
    debug_ffmpeg: bool = False


def argparse_config() -> DaemonConfig:
    parser = argparse.ArgumentParser()
    config = DaemonConfig()
    for dc_field in fields(DaemonConfig):
        if dc_field.type == bool:
            action = argparse.BooleanOptionalAction
        else:
            action = "store"
        if dc_field.name == "upscaling_method":
            choices = UPSCALING_METHODS
        elif dc_field.name == "palette":
            choices = PALETTES.keys()
        else:
            choices = None
        parser.add_argument(
            "--" + dc_field.name,
            type=dc_field.type,
            help=f"{dc_field.type.__name__} (=\t{dc_field.default})",
            action=action,
            choices=choices,
        )

    args = parser.parse_args()
    for arg_name, arg_value in vars(args).items():
        if arg_value is not None:
            setattr(config, arg_name, arg_value)
            logger.info(f"set {arg_name} to {arg_value}")
    return config

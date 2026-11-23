"""动画帧目录服务与真实显示帧数据集生产管线。

另含两个上层模块：

* :mod:`animation_pipeline.puppets`：把合成后的显示画布读成纸样姿势（场记读法）；
* :mod:`animation_pipeline.shoot`：把读法兑换成指定机位的补拍单（两套实拍工位）。
"""

from .catalog import (
    CatalogValidationError,
    FrameCatalog,
    RawFrame,
)
from .compositor import (
    CheckpointCache,
    DisplayFrame,
    FramePlayer,
    effective_delay_centis,
    normalize_time,
    produce_dataset,
    reference_replay,
)
from .gif_io import GifFile, GifImage, frame_spec, parse_gif, write_gif
from .puppets import (
    Hole,
    Interpretation,
    Mark,
    Pattern,
    PuppetReading,
    SceneReading,
    Seed,
    read_scene,
)
from .shoot import (
    Camera,
    Exposure,
    FrameJob,
    RealPuppet,
    ShootPlan,
    Slot,
    Undelivered,
    Worksheet,
    plan_shoot,
    verify_plan,
)

__all__ = [
    "CatalogValidationError",
    "FrameCatalog",
    "RawFrame",
    "CheckpointCache",
    "DisplayFrame",
    "FramePlayer",
    "effective_delay_centis",
    "normalize_time",
    "produce_dataset",
    "reference_replay",
    "GifFile",
    "GifImage",
    "frame_spec",
    "parse_gif",
    "write_gif",
    "Hole",
    "Interpretation",
    "Mark",
    "Pattern",
    "PuppetReading",
    "SceneReading",
    "Seed",
    "read_scene",
    "Camera",
    "Exposure",
    "FrameJob",
    "RealPuppet",
    "ShootPlan",
    "Slot",
    "Undelivered",
    "Worksheet",
    "plan_shoot",
    "verify_plan",
]

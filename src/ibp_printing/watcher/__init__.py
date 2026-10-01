"""Label watcher: print EasyPost labels that volunteers download by hand.

When a USB label printer fails, volunteers download the label image from the
EasyPost website. This app watches the Downloads folder, prints any new label
on the first available label printer, files it under ``printed/`` or
``failed/``, and logs every step so printer failures can be diagnosed later.
"""

from ibp_printing.watcher.config import WatcherConfig, default_config_path, load_config
from ibp_printing.watcher.service import LabelWatcher

__all__ = ["LabelWatcher", "WatcherConfig", "default_config_path", "load_config"]

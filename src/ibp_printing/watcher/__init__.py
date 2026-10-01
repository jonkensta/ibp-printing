"""Label watcher: print shipping labels from Downloads and the to-print queue.

shippy and shippy-gui save a label into ``<Downloads>/to-print/`` when they
cannot print it; the watcher retries that folder whenever a usable printer
exists. It also prints 4x6 labels downloaded by hand into Downloads. Each label
ends up in ``printed/``, stays in (or goes to) ``to-print/`` when it definitely
did not print, or goes to ``check-printer/`` when the printer queue reported a
problem. Every step is logged so printer failures can be diagnosed later.
"""

from ibp_printing.watcher.config import WatcherConfig, default_config_path, load_config
from ibp_printing.watcher.service import LabelWatcher

__all__ = ["LabelWatcher", "WatcherConfig", "default_config_path", "load_config"]

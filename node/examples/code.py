# code.py — the entire drive-side program.
#
# Everything the node does lives in /lib/picotty_node/ (installed as .mpy).
# run() reads settings.toml, builds the cooperative loop, and NEVER returns.
# The only file you edit per node is settings.toml; this file never changes,
# and a library update is "replace /lib/picotty_node/" — it touches neither.

import picotty_node

picotty_node.run()

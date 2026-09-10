"""Reveal file-backed Houdini nodes in the operating system's file browser.

Ported from a Nuke plugin of the same name. Works with any node that has a
file-reference string parameter (Read/File SOPs, ROPs with output paths,
COPs, image parms, etc.) and understands Houdini frame tokens ($F, $F4,
$FF) as well as printf-style (%04d) and hash-style (####) tokens.
"""

from __future__ import print_function

import glob
import os
import re
import shutil
import subprocess
import sys

import hou


_PRINTF_FRAME_RE = re.compile(r"%(?:0(\d+))?d")
_HASH_FRAME_RE = re.compile(r"(#+)")
_HOUDINI_FRAME_RE = re.compile(r"\$F(\d*)")

# Parameter names, in priority order, that commonly hold a file path on
# Houdini nodes. We still fall back to scanning all string parms if none
# of these are present.
_COMMON_FILE_PARMS = (
    "file", "filename", "filename1", "sopoutput", "dopoutput",
    "copoutput", "cop_output", "picture", "vm_picture", "vm_dcmfilename",
    "shopoutput", "lstring", "geo_file", "cache_dir",
)

# Per-node-type overrides for cases where the generic name list above
# would pick the wrong parm (e.g. a Mantra ROP has both "vm_picture" and
# several other file-ish parms; an Alembic ROP uses "filename" for output
# rather than an input). Keyed by node type name as returned by
# node.type().name(). Extend this table as your pipeline needs more node
# types -- it is the main lever for "output-node awareness".
_NODE_TYPE_FILE_PARMS = {
    # ROPs (output drivers)
    "ifd": ("vm_picture",),                    # Mantra
    "karma": ("picture",),                     # Karma
    "opengl": ("picture",),                    # OpenGL ROP
    "alembic": ("filename",),                  # Alembic ROP
    "rop_geometry": ("sopoutput",),             # Geometry/ROP output driver
    "geometry": ("sopoutput",),
    "dop": ("dopoutput",),
    "comp": ("copoutput",),
    # SOPs / object-level inputs
    "file": ("file",),                          # File SOP
    "alembicarchive": ("fileName",),            # Alembic Archive OBJ
}


def _message(text):
    """Show a Houdini message box."""
    hou.ui.displayMessage(str(text))


def _current_frame():
    try:
        return int(round(hou.frame()))
    except Exception:
        return 1


def _find_file_parm(node):
    """Return the most likely file-reference parm on a node, or None."""
    type_name = node.type().name()
    for name in _NODE_TYPE_FILE_PARMS.get(type_name, ()):
        parm = node.parm(name)
        if parm is not None:
            return parm

    for name in _COMMON_FILE_PARMS:
        parm = node.parm(name)
        if parm is not None:
            return parm

    # Fall back: look for any string parm whose template is tagged as a
    # file reference.
    for parm in node.parms():
        template = parm.parmTemplate()
        if template.type() != hou.parmTemplateType.String:
            continue
        if template.stringType() == hou.stringParmType.FileReference:
            return parm

    return None


def _evaluate_file_parm(node):
    parm = _find_file_parm(node)
    if parm is None:
        return ""

    try:
        value = parm.eval()
    except Exception:
        try:
            value = parm.unexpandedString()
        except Exception:
            return ""

    if not value:
        return ""

    path = os.path.expanduser(os.path.expandvars(str(value)))
    return path


def _absolute_path(path):
    if not os.path.isabs(path):
        base = hou.expandString("$HIP")
        path = os.path.join(base, path)
    return os.path.normpath(path)


def _replace_frame_tokens(path, frame):
    def printf_replacer(match):
        width = int(match.group(1) or 0)
        return ("{0:0%dd}" % width).format(frame) if width else str(frame)

    def hash_replacer(match):
        return str(frame).zfill(len(match.group(1)))

    def houdini_replacer(match):
        width = int(match.group(1) or 0)
        return str(frame).zfill(width) if width else str(frame)

    path = _PRINTF_FRAME_RE.sub(printf_replacer, path)
    path = _HASH_FRAME_RE.sub(hash_replacer, path)
    return _HOUDINI_FRAME_RE.sub(houdini_replacer, path)


def _sequence_glob(path):
    pattern = _PRINTF_FRAME_RE.sub("*", path)
    pattern = _HASH_FRAME_RE.sub("*", pattern)
    pattern = _HOUDINI_FRAME_RE.sub("*", pattern)
    return pattern


def _best_existing_target(path):
    """Return a real frame/file when possible, otherwise the requested path."""
    path = _absolute_path(path)
    current_frame_path = _replace_frame_tokens(path, _current_frame())
    if os.path.isfile(current_frame_path):
        return current_frame_path

    pattern = _sequence_glob(path)
    if pattern != path:
        matches = sorted(item for item in glob.glob(pattern) if os.path.isfile(item))
        if matches:
            return matches[0]

    return current_frame_path


def _open_in_file_browser(target):
    directory = target if os.path.isdir(target) else os.path.dirname(target)
    if not directory or not os.path.isdir(directory):
        raise OSError("Folder does not exist:\n{0}".format(directory or target))

    target_exists = os.path.isfile(target)

    if sys.platform.startswith("win"):
        if target_exists:
            subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(target)])
        else:
            os.startfile(os.path.normpath(directory))
        return

    if sys.platform == "darwin":
        command = ["open", "-R", target] if target_exists else ["open", directory]
        subprocess.Popen(command)
        return

    opener = shutil.which("xdg-open") or shutil.which("gio")
    if opener is None:
        raise OSError("No supported Linux file browser command was found.")

    if os.path.basename(opener) == "gio":
        subprocess.Popen([opener, "open", directory])
    else:
        subprocess.Popen([opener, directory])


def reveal_selected_file_locations(**kwargs):
    """Reveal files referenced by the selected Houdini nodes."""
    nodes = hou.selectedNodes()
    if not nodes:
        _message("Select a node with a file parameter first.")
        return

    targets = []
    skipped = []
    for node in nodes:
        path = _evaluate_file_parm(node)
        if not path:
            skipped.append(node.name())
            continue
        targets.append(_best_existing_target(path))

    if not targets:
        _message("The selected node does not contain a usable file path.")
        return

    # Open only one target per folder so selecting many nodes does not
    # create duplicate Explorer/Finder windows.
    unique_targets = {}
    for target in targets:
        folder = target if os.path.isdir(target) else os.path.dirname(target)
        unique_targets.setdefault(os.path.normcase(folder), target)

    failures = []
    for target in unique_targets.values():
        try:
            _open_in_file_browser(target)
        except Exception as exc:
            failures.append(str(exc))

    if failures:
        _message("Could not reveal the file location:\n\n" + "\n".join(failures))
    elif skipped and len(nodes) == 1:
        _message("The selected node does not contain a usable file path.")


def copy_selected_file_paths(resolved=True, **kwargs):
    """Copy the file path(s) of the selected node(s) to the clipboard.

    resolved=True copies the path with variables/frame tokens expanded to
    the current frame (what you'd actually find on disk). resolved=False
    copies the raw, unexpanded parm string (e.g. still containing $F4).
    Multiple selected nodes are joined with newlines.
    """
    nodes = hou.selectedNodes()
    if not nodes:
        _message("Select a node with a file parameter first.")
        return

    lines = []
    skipped = []
    for node in nodes:
        parm = _find_file_parm(node)
        if parm is None:
            skipped.append(node.name())
            continue
        if resolved:
            path = _evaluate_file_parm(node)
            if path:
                path = _replace_frame_tokens(_absolute_path(path), _current_frame())
        else:
            try:
                path = parm.unexpandedString()
            except Exception:
                path = ""
        if path:
            lines.append(path)
        else:
            skipped.append(node.name())

    if not lines:
        _message("The selected node does not contain a usable file path.")
        return

    text = "\n".join(lines)
    try:
        hou.ui.copyTextToClipboard(text)
    except Exception as exc:
        _message("Could not copy to clipboard:\n\n{0}".format(exc))
        return

    if skipped:
        _message(
            "Copied {0} path(s) to clipboard.\nSkipped (no file parm): {1}"
            .format(len(lines), ", ".join(skipped))
        )


_MISSING_FILE_COLOR = hou.Color((1.0, 0.15, 0.15))


def highlight_missing_files(nodes=None, **kwargs):
    """Color selected nodes red if their referenced file/sequence is missing.

    Nodes whose file resolves to an existing file are left untouched (run
    clear_missing_file_highlight to reset colors). Nodes with no file parm
    at all are skipped silently, since they aren't file-backed to begin
    with.
    """
    nodes = nodes if nodes is not None else hou.selectedNodes()
    if not nodes:
        _message("Select one or more nodes to check first.")
        return

    missing = []
    checked = 0
    for node in nodes:
        parm = _find_file_parm(node)
        if parm is None:
            continue
        checked += 1
        raw_path = _evaluate_file_parm(node)
        if not raw_path:
            continue
        target = _best_existing_target(raw_path)
        if not os.path.isfile(target) and not os.path.isdir(target):
            node.setColor(_MISSING_FILE_COLOR)
            missing.append(node.name())

    if checked == 0:
        _message("None of the selected nodes have a file parameter.")
    elif missing:
        _message(
            "Missing file(s) on {0} node(s):\n\n{1}"
            .format(len(missing), "\n".join(missing))
        )
    else:
        _message("All {0} checked node(s) have valid files on disk.".format(checked))


def clear_missing_file_highlight(nodes=None, **kwargs):
    """Reset node color back to its node type's default for selected nodes."""
    nodes = nodes if nodes is not None else hou.selectedNodes()
    if not nodes:
        _message("Select one or more nodes to reset first.")
        return

    for node in nodes:
        try:
            node.setColor(node.type().defaultColor())
        except Exception:
            pass


__all__ = [
    "reveal_selected_file_locations",
    "copy_selected_file_paths",
    "highlight_missing_files",
    "clear_missing_file_highlight",
]

"""Post-install: check if 'travian' command is on PATH and offer to fix it.

Run automatically after install, or manually: python -m travian_api._post_install
"""

import os
import shutil
import sys
import sysconfig


def get_scripts_dir() -> str:
    """Get the directory where pip installed the 'travian' entry point."""
    exe = "travian.exe" if os.name == "nt" else "travian"

    # Check user scripts first
    user_scripts = sysconfig.get_path("scripts", "nt_user" if os.name == "nt" else "posix_user")
    if user_scripts and os.path.exists(os.path.join(user_scripts, exe)):
        return user_scripts

    # Then global scripts
    global_scripts = sysconfig.get_path("scripts")
    if global_scripts and os.path.exists(os.path.join(global_scripts, exe)):
        return global_scripts

    return ""


def is_on_path(directory: str) -> bool:
    """Check if a directory is on PATH."""
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    norm = os.path.normcase(os.path.normpath(directory))
    return any(os.path.normcase(os.path.normpath(d)) == norm for d in path_dirs if d)


def add_to_user_path_windows(directory: str) -> bool:
    """Add directory to Windows user PATH permanently (no admin needed)."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
        ) as key:
            try:
                current, _ = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current = ""

            dirs = [d.strip() for d in current.split(";") if d.strip()]
            norm = os.path.normcase(os.path.normpath(directory))
            if any(os.path.normcase(os.path.normpath(d)) == norm for d in dirs):
                return True  # already there

            dirs.append(directory)
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, ";".join(dirs))

        # Broadcast so new terminals pick it up immediately
        try:
            import ctypes

            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None
            )
        except Exception:
            pass

        return True
    except Exception:
        return False


def main():
    """Check PATH and fix if needed."""
    if shutil.which("travian"):
        print("[OK] 'travian' command is ready.")
        return

    scripts_dir = get_scripts_dir()
    if not scripts_dir:
        print("Tip: use 'python -m travian_api' if 'travian' isn't found.")
        return

    if is_on_path(scripts_dir):
        print("[OK] PATH is set. Reopen your terminal for 'travian' to work.")
        return

    print(f"'travian' was installed to: {scripts_dir}")
    print("This directory is not on your PATH.\n")

    if os.name == "nt":
        if sys.stdin.isatty():
            try:
                answer = input("Add it to PATH automatically? [Y/n]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
        else:
            answer = "y"  # non-interactive: just do it

        if answer in ("", "y", "yes"):
            if add_to_user_path_windows(scripts_dir):
                print("[OK] Added to user PATH. Reopen your terminal and 'travian' will work.")
            else:
                print("Could not update PATH automatically.")
                print(f"Manual fix — add this to your PATH: {scripts_dir}")
        else:
            print("No problem. Use: python -m travian_api")
    else:
        shell = os.environ.get("SHELL", "/bin/bash")
        rc = "~/.bashrc" if "bash" in shell else "~/.zshrc"
        print(f'Add to {rc}:  export PATH="$PATH:{scripts_dir}"')
        print("Or use: python -m travian_api")


if __name__ == "__main__":
    main()

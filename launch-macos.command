#!/bin/zsh

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
GUI_SCRIPT="$SCRIPT_DIR/codex56-control-center.py"
PYTHON_CMD=""

show_ai_install_prompt() {
    local issue="$1"
    local prompt_text
    prompt_text="请帮我在这台 macOS 电脑上安装或修复 ${issue}。安装完成后，请运行 '${SCRIPT_DIR}/launch-macos.command' 重新打开软件。"

    if command -v pbcopy >/dev/null 2>&1; then
        printf '%s' "$prompt_text" | pbcopy
    fi
    if command -v osascript >/dev/null 2>&1; then
        osascript - "$issue" "$prompt_text" <<'APPLESCRIPT'
on run argv
    set issueText to item 1 of argv
    set promptText to item 2 of argv
    set the clipboard to promptText
    display dialog "缺少 " & issueText & "。安装提示词已复制，请打开 Codex 直接粘贴发送。" buttons {"知道了"} default button "知道了" with title "需要配置运行环境"
end run
APPLESCRIPT
    else
        echo ""
        echo "请复制下面的提示词并发送给 Codex："
        echo ""
        echo "$prompt_text"
        read -k 1 "REPLY?按任意键关闭..."
        echo
    fi
}

if [[ ! -f "$GUI_SCRIPT" ]]; then
    echo "[ERROR] GUI script was not found:"
    echo "$GUI_SCRIPT"
    read -k 1 "REPLY?Press any key to close..."
    echo
    exit 1
fi

for candidate in \
    python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
    python
do
    if [[ "$candidate" == */* ]]; then
        [[ -x "$candidate" ]] || continue
    else
        command -v "$candidate" >/dev/null 2>&1 || continue
    fi
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        PYTHON_CMD="$candidate"
        break
    fi
done

if [[ -z "$PYTHON_CMD" ]]; then
    show_ai_install_prompt "Python 3.10 或更高版本"
    exit 1
fi

if ! "$PYTHON_CMD" - <<'PYTHON_CHECK' >/dev/null 2>&1
import tkinter as tk

root = tk.Tk()
root.withdraw()
root.update_idletasks()
root.destroy()
PYTHON_CHECK
then
    show_ai_install_prompt "Python 图形组件 Tkinter"
    exit 1
fi

echo "[Environment]"
"$PYTHON_CMD" --version
echo "[Starting] $GUI_SCRIPT"

cd "$SCRIPT_DIR" || exit 1
exec "$PYTHON_CMD" "$GUI_SCRIPT"

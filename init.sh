if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
  echo "Attaching to existing tmux session: $SESSION_NAME"
  exec tmux attach -t "$SESSION_NAME"
else
    tmux new-session -d -s "ai" "source .venv/bin/activate && python -m src.main"
    # Attach to it
    exec tmux attach -t "ai"
fi
Output assets/demo.gif

Set Shell "bash"
Set FontSize 14
Set Width 900
Set Height 600
Sleep 500ms

Type "cd /c/Users/User/Documents/Projects/agenthold"
Enter
Sleep 300ms

Type "echo '=== WITHOUT AGENTHOLD ==='"
Enter
Sleep 500ms
Type "uv run python examples/order_processing/without_agenthold.py"
Enter
Sleep 4s

Type ""
Enter
Sleep 300ms
Type "echo '=== WITH AGENTHOLD ==='"
Enter
Sleep 500ms
Type "uv run python examples/order_processing/with_agenthold.py"
Enter
Sleep 4s

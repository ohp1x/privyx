# Renders docs/assets/audit.gif. Run from the repo root, with ports 8000 and
# 9000 free, and curl installed:
#   vhs scripts/docs-assets/audit.tape

Output docs/assets/audit.gif

Set Shell "bash"
Set FontSize 18
Set Width 1400
Set Height 480
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms

# A few requests through the proxy so the audit log has something to
# summarize. Each key is written in two parts so that this file holds no
# key-shaped string.
Hide
Type `unset PROMPT_COMMAND; PS1='$ '; R=$PWD; cd "$(mktemp -d)"`
Enter
Type `python "$R/scripts/docs-assets/stub_upstream.py" 9000 & privyx proxy --upstream http://127.0.0.1:9000 >proxy.out 2>&1 &`
Enter
Type `until curl -s -o /dev/null localhost:8000/health; do sleep 0.2; done`
Enter
Sleep 5s
Type `A=AKIA; G=ghp_; for m in "mail ann@example.com" "deploy to 10.0.4.17 with ${A}IOSFODNN7EXAMPLE" "auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dGVzdHNpZ25hdHVyZQ for bob@example.com" "token ${G}1A2b3C4d5E6f7G8h9I0jK1lM2nO3pQ4rS5tU"; do curl -s -o /dev/null --json "{\"model\":\"gpt-4o\",\"messages\":[{\"role\":\"user\",\"content\":\"$m\"}]}" localhost:8000/v1/chat/completions; done; clear`
Enter
Sleep 3s
Show

Type "# Counts what was masked; never stores the values"
Enter
Type "privyx audit stats"
Sleep 300ms
Enter
Sleep 5s

Hide
Type "kill %1 %2"
Enter

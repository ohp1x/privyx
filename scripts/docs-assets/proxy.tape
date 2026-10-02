# Renders docs/assets/proxy.gif. Run from the repo root, with ports 8000 and
# 9000 free, and curl and jq installed:
#   vhs scripts/docs-assets/proxy.tape

Output docs/assets/proxy.gif

Set Shell "bash"
Set FontSize 18
Set Width 1400
Set Height 420
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms

# The request is written here, with the key in two parts, so that this file
# holds no key-shaped string.
Hide
Type `unset PROMPT_COMMAND; PS1='$ '; R=$PWD; cd "$(mktemp -d)"`
Enter
Type `printf '{\n  "model": "gpt-4o",\n  "messages": [\n    {"role": "user", "content": "Rotate %s%s and email ann@example.com"}\n  ]\n}\n' sk-proj- Ab3dEf5gHi7jKl9mNo1pQr3sTu5vWx7y > request.json`
Enter
Type `python "$R/scripts/docs-assets/stub_upstream.py" 9000 & privyx proxy --upstream http://127.0.0.1:9000 >proxy.out 2>&1 &`
Enter
Type `until curl -s -o /dev/null localhost:8000/health; do sleep 0.2; done; clear`
Enter
Sleep 5s
Show

Type "# Privyx on :8000, in front of a stub provider on :9000 that logs and echoes each message"
Enter
Type "cat request.json"
Sleep 300ms
Enter
Sleep 2s

Type `curl -s --json @request.json localhost:8000/v1/chat/completions | jq -r '.choices[0].message.content'`
Sleep 300ms
Enter
Sleep 2.5s

Type "# What the provider received"
Enter
Type "cat provider.log"
Sleep 300ms
Enter
Sleep 4s

Hide
Type "kill %1 %2"
Enter

# Renders docs/assets/demo.gif. Run from the repo root:
#   vhs scripts/docs-assets/demo.tape
# Needs vhs (https://github.com/charmbracelet/vhs), which needs ttyd and ffmpeg,
# and `privyx` on PATH.

Output docs/assets/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1400
Set Height 360
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms

# Start in an empty directory so the demo runs on the default config and never
# reuses an old map.json. PS1 is reset because bash inherits whatever prompt
# the calling shell exported. The key is written in two parts so that this
# file holds no key-shaped string.
Hide
Type `unset PROMPT_COMMAND; PS1='$ '; cd "$(mktemp -d)"`
Enter
Type `printf 'Deploy with OPENAI_API_KEY=%s%s, then mail ann@example.com\n' sk-proj- Ab3dEf5gHi7jKl9mNo1pQr3sTu5vWx7y > prompt.txt; clear`
Enter
Sleep 1s
Show

Type "# A prompt with a key and an email address in it"
Enter
Type "cat prompt.txt"
Sleep 300ms
Enter
Sleep 2s

Type "# What the provider receives"
Enter
Type "privyx mask --map map.json -i prompt.txt"
Sleep 500ms
Enter
Sleep 2.5s

Type "# What you get back from the model's reply"
Enter
Type 'privyx unmask --map map.json "Deployed. Sent the summary to <PRIVYX_EMAIL_2>."'
Sleep 500ms
Enter
Sleep 4s

# Renders docs/assets/detect.gif. Run from the repo root:
#   vhs scripts/docs-assets/detect.tape

Output docs/assets/detect.gif

Set Shell "bash"
Set FontSize 18
Set Width 1400
Set Height 480
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms

# The log is written here, with each key in two parts, so that this file holds
# no key-shaped string.
Hide
Type `unset PROMPT_COMMAND; PS1='$ '; cd "$(mktemp -d)"`
Enter
Type `printf '%s\n' '09:12:44 ERROR payment failed user=ann@example.com ip=10.0.4.17 card=4111 1111 1111 1111' '09:12:44 DEBUG retry auth=Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.dGVzdHNpZ25hdHVyZQ' > app.log`
Enter
Type `printf '09:12:45 DEBUG GITHUB_TOKEN=%s%s AWS_ACCESS_KEY_ID=%s%s\n' ghp_ 1A2b3C4d5E6f7G8h9I0jK1lM2nO3pQ4rS5tU AKIA IOSFODNN7EXAMPLE >> app.log; clear`
Enter
Sleep 1s
Show

Type "# A log you are about to paste into a chat"
Enter
Type "cat app.log"
Sleep 300ms
Enter
Sleep 2.5s

Type "# What Privyx finds, and what the model gets instead"
Enter
Type "privyx detect --transform --stdin < app.log"
Sleep 300ms
Enter
Sleep 5s

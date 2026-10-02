# Renders docs/assets/terms.gif. Run from the repo root:
#   vhs scripts/docs-assets/terms.tape

Output docs/assets/terms.gif

Set Shell "bash"
Set FontSize 18
Set Width 1400
Set Height 440
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 30ms

Hide
Type `unset PROMPT_COMMAND; PS1='$ '; cd "$(mktemp -d)"`
Enter
Type `printf '%s\n' 'detector:' '  type: regex' '  terms:' '    PERSON:       [ann, bob]' '    ORGANIZATION: [acme, initech]' '    PROJECT:      [bluebird]' > my.yaml; clear`
Enter
Sleep 1s
Show

Type "# Your own names, clients, and codenames: a plain word list"
Enter
Type "cat my.yaml"
Sleep 300ms
Enter
Sleep 2.5s

Type 'privyx detect -c my.yaml --transform "Bob at Initech wants Bluebird specs before Acme does"'
Sleep 300ms
Enter
Sleep 5s

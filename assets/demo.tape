# Demo recording for jp, rendered with vhs (https://github.com/charmbracelet/vhs).
#
#   brew install vhs          # or: go install github.com/charmbracelet/vhs@latest
#   vhs assets/demo.tape      # writes assets/demo.gif
#
# Then add it to the README, e.g. just under the badges:
#   <p align="center"><img src="assets/demo.gif" alt="jp demo" width="680"></p>
#
# This tape only runs commands that work without a live server, so it is safe to
# record anywhere jp is installed. For a richer demo, record a real clone/push/pull
# against your JupyterHub in a throwaway folder (never commit the token).

Output assets/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1000
Set Height 640
Set Padding 22
Set Theme "GitHub Dark"
Set TypingSpeed 60ms

Sleep 600ms
Type "jp --version"
Sleep 400ms
Enter
Sleep 1.2s

Type "jp --help"
Sleep 400ms
Enter
Sleep 3.5s

Type "# clone a JupyterHub folder, edit locally, push to the GPUs, pull results"
Sleep 400ms
Enter
Sleep 1.5s

Type "jp doctor"
Sleep 400ms
Enter
Sleep 2.5s

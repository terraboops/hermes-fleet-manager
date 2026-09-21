#!/usr/bin/env python3
"""Post-crash kick: dispatch a SHORT resume/status prompt to every managed CC session
(the example session intentionally excluded — it gets its own task-specific kick)."""
import json, os, subprocess, time, sys

REG = os.path.expanduser('~/.hermes/scripts/cc-watch/fleet_registry.json')


def main():
    d = json.load(open(REG))
    MESSAGE = ("Post-crash: your session was restored. Give a one-line status of your current "
               "task and continue it. If you were mid-task, resume from where you left off.")
    KICK = ('/tmp/kick_msg.txt')
    open(KICK, 'w').write(MESSAGE + '\n')

    for e in d['sessions']:
        name = e['name']
        if name == 'cc-w-example-1234':
            continue   # this session gets its own kick
        subprocess.run(['tmux','send-keys','-t',"=" + name,'C-c'], capture_output=True)
        time.sleep(0.4)
        subprocess.run(['tmux','load-buffer','-b','k', KICK], capture_output=True)
        time.sleep(0.3)
        subprocess.run(['tmux','paste-buffer','-b','k','-t',name], capture_output=True)
        time.sleep(0.4)
        subprocess.run(['tmux','send-keys','-t',"=" + name,'Enter'], capture_output=True)
        time.sleep(1.5)
        print(f"{name:<20} kicked")


if __name__ == '__main__':
    main()

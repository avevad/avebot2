#!/usr/bin/env python3
from telethon import TelegramClient, events, tl, errors
import asyncio, sys, os, re, pty, signal

BOT_NAME = 'avebot'
BOT_VERSION = '2.2'

API_ID = os.environ['API_ID']
API_HASH = os.environ['API_HASH']

BUF_LEN = 2048
TERM_W, TERM_H = 80, 25

last_code = None

class Terminal:
    def __init__(self, w, h):
        self.w = w
        self.h = h
        self.rows = []
        self.i = 0
        self.j = 0
        self.cursor = True

    def cursor_down(self):
        self.i += 1
        if self.i == self.h:
            if len(self.rows) != 0:
                self.rows.pop(0)
            self.i -= 1

    def new_line(self):
        self.j = 0
        self.cursor_down()

    def putc(self, c):
        if c == '\n':
            self.new_line()
            return
        if c == '\r':
            self.j = 0
            return
        if self.j >= self.w:
            return
        while self.i >= len(self.rows):
            self.rows.append([])
        while self.j >= len(self.rows[self.i]):
            self.rows[self.i].append(' ')
        self.rows[self.i][self.j] = c
        self.j += 1
        if self.j == self.w and self.cursor:
            self.new_line()

    def do_escape(self, esc):
        if len(esc) < 2:
            return # unknown
        if esc == '[?25l':
            self.cursor = False
            return
        if esc == '[?25h':
            self.cursor = True
        if esc[0] == '[':
            num = None
            try:
                num = int(esc[1:-1])
            except ValueError:
                pass
            if esc[-1] == 'm':
                return # graphics - unsupported
            if num is not None:
                if esc[-1] == 'A': self.i = max(0, self.i - num)
                if esc[-1] == 'B':
                    for _ in range(num):
                        self.cursor_down()
                if esc[-1] == 'C': self.j = min(self.w - 1, self.j + num)
                if esc[-1] == 'D': self.j = max(0, self.j - num)
                if self.i == 0 and self.j == 0:
                    print(list(esc))

    def puts(self, s):
        esc = None
        for c in s:
            if c == '\x1B':
                esc = ""
                continue
            if esc is not None:
                esc += c
                if len(esc) == 1:
                    if c in '[ ': continue
                else:
                    if c in '0123456789;?': continue
                self.do_escape(esc)
                esc = None
                continue
            self.putc(c)
        if esc is not None:
            return '\x1B' + esc
        return ''

    def to_string(self):
        result = '\n'.join([''.join(row) for row in self.rows])
        if result[0] == ' ':
            result = '.' + result[1:]
        result = result.strip()
        return result

def tg_len(text):
    return len(text.encode('utf-16-le')) // 2

class ProcHandle:
    def __init__(self, proc, term):
        self.proc = proc
        self.term = term

procs = dict()

async def edit_message(msg, text):
    try:
        await msg.edit(text, formatting_entities=[tl.types.MessageEntityCode(offset=0, length=tg_len(text))])
    except errors.rpcerrorlist.MessageNotModifiedError:
        pass
    except errors.FloodWaitError as ex:
        await asyncio.sleep(ex.seconds)

CMD_LEN = 10

async def handle_shell_command(msg):
    cmd = msg.raw_text[1:].strip()
    term = Terminal(TERM_W, TERM_H)
    env = dict(os.environ)
    me = await msg.client.get_me()
    env["AVEBOT_VERSION"] = BOT_VERSION
    if me.username is not None: env["AVEBOT_USERNAME"] = me.username
    env["AVEBOT_NAME"] = f"{me.first_name} {me.last_name}" if me.last_name is not None else me.first_name
    env["AVEBOT_ID"] = str(me.id)
    env["AVEBOT_CHAT_ID"] = str((await msg.get_chat()).id)
    global last_code
    if last_code is not None: env["AVEBOT_LAST_CODE"] = last_code
    if msg.is_reply:
        rep = await msg.get_reply_message()
        if rep.raw_text is not None: env["AVEBOT_REPLY"] = rep.raw_text
    proc = await asyncio.create_subprocess_shell(cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env)
    term.puts(f"$ {cmd}\n")
    procs[(msg.chat_id, msg.id)] = ProcHandle(proc, term)
    output = ''
    while True:
        await edit_message(msg, term.to_string())
        output += (await proc.stdout.read(BUF_LEN)).decode(errors='ignore')
        end = len(output) == 0
        output = term.puts(output)
        if end: break
    await proc.wait()
    if len(cmd) > CMD_LEN: cmd = cmd[:CMD_LEN - 1] + '…'
    term.puts(f"\n{BOT_NAME}: command `{cmd}` finished with exit code {proc.returncode}")
    await edit_message(msg, term.to_string())

async def handle_terminal_edit(msg):
    pr = procs[(msg.chat_id, msg.id)]
    old_len = len(pr.term.to_string())
    new_len = len(msg.raw_text)
    if new_len > old_len:
        inp = msg.raw_text[-(new_len - old_len):].strip('\n')
        if inp == '🔚':
            pr.proc.stdin.close()
            return
        if inp == '🛑':
            pr.proc.send_signal(signal.SIGTERM)
            return
        if inp in '☠️💀':
            pr.proc.send_signal(signal.SIGKILL)
            return
        inp += '\n'
        pr.term.puts(inp)
        await edit_message(msg, pr.term.to_string())
        pr.proc.stdin.write(inp.encode())

async def handle_short_code(msg):
    if msg.raw_text is not None:
        expr = re.compile("Login code: \d\d\d\d\d")
        match = expr.search(msg.raw_text)
        global last_code
        if match: last_code = match.group(0)


async def client_loop(client):
    client.on(events.NewMessage(
        from_users=(await client.get_me()),
        forwards=False,
        pattern=re.compile('^[\$\;\,].+$')
    ))(handle_shell_command)
    client.on(events.MessageEdited(
        func=lambda msg: (msg.chat_id, msg.id) in procs
    ))(handle_terminal_edit)
    client.on(events.NewMessage(
        chats=[777000]
    ))(handle_short_code)
    await client.run_until_disconnected()

async def main():
    try:
        os.mkdir("sessions")
    except FileExistsError:
        pass
    phones = os.environ['PHONES'].split(':')
    clients = []
    for phone in phones:
        print(f"Authenticating {phone}")
        client = TelegramClient(f"sessions/{phone}", api_id=API_ID, api_hash=API_HASH)
        await client.start(phone)
        clients.append(client)
    loops = [client_loop(client) for client in clients]
    print(f"{BOT_NAME} v{BOT_VERSION} started!")
    await asyncio.gather(*loops)

if __name__ == '__main__':
    if "AVEBOT_VERSION" in os.environ:
        print(f"{BOT_NAME} v{BOT_VERSION} is running")
        print("-----")
        print(f"Account: {os.environ['AVEBOT_NAME']}")
        if "AVEBOT_USERNAME" in os.environ:
            print(f"Username: @{os.environ['AVEBOT_USERNAME']}")
        print(f"ID: {os.environ['AVEBOT_ID']}")
        print(f"Chat: {os.environ['AVEBOT_CHAT_ID']}")
    else:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            pass

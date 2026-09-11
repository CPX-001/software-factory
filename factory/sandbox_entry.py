"""Private Linux namespace bootstrap. Never accepts input from a product/worker.

Executed by unshare before any untrusted import. Mounts are an allowlist, not cwd
isolation. The bootstrap and its input stay outside the resulting root.
"""
import ctypes
import errno
import json
import os
from pathlib import Path
import resource
import sys


def enter(spec):
    libc = ctypes.CDLL(None, use_errno=True)
    def mount(source, target, kind=None, flags=0, data=None):
        args = [os.fsencode(x) if x is not None else None for x in (source, target, kind)]
        if libc.mount(*args, ctypes.c_ulong(flags), os.fsencode(data) if data else None):
            raise OSError(ctypes.get_errno(), 'namespace mount failed: ' + str(target))
    mount(None, '/', flags=(1 << 18) | (1 << 14))  # private, recursive
    root = Path(spec['root'])
    mount('tmpfs', str(root), 'tmpfs', data='size=256m,mode=755')
    def bind(source, destination, writable=False):
        source = Path(source).resolve(strict=True)
        target = root / destination.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            target.mkdir(exist_ok=True)
        else:
            target.touch()
        mount(str(source), str(target), flags=4096 | 16384)  # recursive bind (locked host submounts)
        class MountAttr(ctypes.Structure):
            _fields_ = [('set', ctypes.c_uint64), ('clear', ctypes.c_uint64),
                        ('propagation', ctypes.c_uint64), ('userns_fd', ctypes.c_uint64)]
        attr = MountAttr(2 | 4 | (0 if writable else 1), 0, 0, 0)
        # Add restrictions without clearing locked atime flags inherited from a host sandbox.
        if libc.syscall(442, -100, os.fsencode(target), 0x8000, ctypes.byref(attr), ctypes.sizeof(attr)):
            raise OSError(ctypes.get_errno(), 'Cannot restrict bind mount: ' + str(target))
    for directory in ('/usr', '/bin', '/lib', '/lib64'):
        if Path(directory).exists():
            bind(directory, directory)
    for src, dst, writable in spec['mounts']:
        bind(src, dst, writable)
    for directory in ('tmp', 'proc', 'dev', 'workspace', 'home'):
        (root / directory).mkdir(exist_ok=True)
    for device in ('null', 'zero', 'urandom', 'random'):
        # Devices must retain device access; bind individually, without nodev.
        target = root / 'dev' / device
        target.touch()
        mount('/dev/' + device, str(target), flags=4096)
    mount('proc', str(root / 'proc'), 'proc', flags=2 | 4 | 8)
    os.chroot(root)
    os.chdir('/workspace')
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (16 * 1024 * 1024, 16 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    if spec['mode'] == 'test':
        resource.setrlimit(resource.RLIMIT_AS, (1024 ** 3, 1024 ** 3))
    # Drop all capabilities, including the namespace CAP_SYS_ADMIN/CAP_SYS_CHROOT.
    class Header(ctypes.Structure):
        _fields_ = [('version', ctypes.c_uint), ('pid', ctypes.c_int)]
    class Caps(ctypes.Structure):
        _fields_ = [('effective', ctypes.c_uint), ('permitted', ctypes.c_uint), ('inheritable', ctypes.c_uint)]
    if libc.capset(ctypes.byref(Header(0x20080522, 0)), (Caps * 2)()) != 0:
        raise OSError('Cannot drop sandbox capabilities')
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # no_new_privs
        raise OSError('Cannot set no_new_privs')
    sec = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    sec.seccomp_init.restype = ctypes.c_void_p
    sec.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
    sec.seccomp_load.argtypes = [ctypes.c_void_p]
    sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    ctx = sec.seccomp_init(0x7fff0000)  # allow, with explicit escape syscalls denied
    if not ctx:
        raise OSError('Cannot initialize seccomp')
    for name in ('mount', 'umount2', 'pivot_root', 'chroot', 'setns', 'unshare',
                 'ptrace', 'process_vm_writev', 'process_vm_readv', 'bpf', 'userfaultfd',
                 'open_by_handle_at', 'keyctl', 'add_key', 'request_key', 'reboot'):
        nr = sec.seccomp_syscall_resolve_name(name.encode())
        if nr >= 0 and sec.seccomp_rule_add(ctx, 0x50000 | errno.EPERM, nr, 0):
            raise OSError('Cannot install seccomp rule')
    if spec['mode'] == 'runtime':
        # Codex may start native threads, but cannot launch shell commands, hooks,
        # child models or any other executable process in this mode.
        for name, error in (('fork', errno.EPERM), ('vfork', errno.EPERM), ('clone3', errno.ENOSYS)):
            nr = sec.seccomp_syscall_resolve_name(name.encode())
            if nr >= 0 and sec.seccomp_rule_add(ctx, 0x50000 | error, nr, 0):
                raise OSError('Cannot prevent runtime child processes')
        class Comparison(ctypes.Structure):
            _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                        ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]
        # clone without CLONE_THREAD is a process. SCMP_CMP_MASKED_EQ = 7.
        sec.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int,
                                               ctypes.c_uint, ctypes.POINTER(Comparison)]
        if sec.seccomp_rule_add_array(ctx, 0x50000 | errno.EPERM,
                sec.seccomp_syscall_resolve_name(b'clone'), 1, ctypes.byref(Comparison(0, 7, 0x10000, 0))):
            raise OSError('Cannot restrict runtime clone')
    if sec.seccomp_load(ctx):
        raise OSError('Cannot load seccomp')
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/home', 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1',
           'CODEX_HOME': '/home', 'OPENAI_API_KEY': '', 'CODEX_API_KEY': ''}
    os.execve(spec['argv'][0], spec['argv'], env)


if __name__ == '__main__':
    try:
        enter(json.loads(Path(sys.argv[1]).read_text()))
    except BaseException as exc:
        print('FACTORY_SANDBOX_UNAVAILABLE: ' + str(exc), file=sys.stderr)
        sys.exit(125)

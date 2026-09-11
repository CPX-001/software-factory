"""Managed Git worktrees, content identities and recoverable local publication."""
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import ast
from contextlib import contextmanager
import selectors

from .execution_contract import PROTECTED, allowed, relative_path
from .registry import FactoryError


def git(repo, *args, input=None, env=None):
    # Never run repository hooks, shell aliases, filters, signing or external diffs.
    result = subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgSign=false',
        '-c', 'core.fsmonitor=false', '-c', 'core.attributesFile=/dev/null', '-C', str(repo), *args],
        input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
             'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0', **(env or {})})
    if result.returncode:
        raise FactoryError('git_failed', result.stderr.decode(errors='replace')[:1000])
    return result.stdout.decode().strip()


def repository_identity(project):
    project = Path(project).resolve()
    top = Path(git(project, 'rev-parse', '--show-toplevel')).resolve()
    common = Path(git(project, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    if top != project or not common.is_relative_to(project):
        raise FactoryError('repository_mismatch', 'Authorize the primary Git repository root, not a nested directory/worktree')
    head = git(project, 'rev-parse', 'HEAD')
    # Git add/checkout can execute filters. Fail closed rather than executing local config.
    config = git(project, 'config', '--local', '--list')
    if any(line.startswith(('filter.', 'include.', 'includeif.', 'core.sshcommand', 'core.worktree')) for line in config.lower().splitlines()):
        raise FactoryError('unsafe_git_config', 'Repository filters/includes/worktree overrides are not supported by execution')
    tracked = git(project, 'ls-tree', '-r', '--name-only', head).splitlines()
    for name in tracked:
        if any(part in PROTECTED for part in Path(name).parts):
            raise FactoryError('protected_tracked_path', 'Controller/configuration paths must not be tracked in the product baseline')
    st = common.stat()
    return {'path': str(top), 'git_dir': str(common), 'device': st.st_dev, 'inode': st.st_ino}


def files(root):
    root = Path(root)
    found = {}
    total = 0
    for parent, directories, names in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if d not in PROTECTED)
        for name in sorted(names + [d for d in directories if (Path(parent) / d).is_symlink()]):
            path = Path(parent) / name
            relative = path.relative_to(root).as_posix()
            if name == '.git':
                continue
            relative_path(relative)
            mode = path.lstat().st_mode
            if not stat.S_ISREG(mode):
                raise FactoryError('unsafe_worktree', 'Symlinks and special files are not supported: ' + relative)
            size = path.stat().st_size
            total += size
            if size > 10_000_000 or total > 100_000_000 or len(found) > 10000:
                raise FactoryError('worktree_too_large', 'Initial execution supports at most 100 MB / 10000 files')
            found[relative] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                               'executable': bool(mode & stat.S_IXUSR)}
    return found


def code_identity(worktree):
    from .architecture import fingerprint
    return fingerprint(files(worktree))


def repair_identity(worktree):
    """Ignore Python comments/formatting when deciding whether another repair is useful."""
    from .architecture import fingerprint
    inventory = files(worktree)
    relevant = {}
    for path, meta in inventory.items():
        if path.endswith('.py'):
            try:
                relevant[path] = ast.dump(ast.parse((Path(worktree) / path).read_text()), include_attributes=False)
            except (SyntaxError, UnicodeError):
                relevant[path] = meta
    return fingerprint(relevant or inventory)


def prepare(store, data):
    repo = store.project
    path = Path(data['worktree'])
    if path.exists():
        if path.is_symlink() or path.resolve() != path or git(path, 'branch', '--show-current') != data['branch']:
            raise FactoryError('worktree_mismatch', 'Existing worktree is not owned by this execution')
        common = Path(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
        if str(common) != data['repository']['git_dir']:
            raise FactoryError('worktree_mismatch', 'Worktree points at another repository')
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Crash after branch creation but before worktree creation: reuse only exact base.
        refs = git(repo, 'for-each-ref', '--format=%(objectname)', 'refs/heads/' + data['branch'])
        if refs and refs != data['base_commit']:
            raise FactoryError('publication_conflict', 'Managed branch has an unexpected commit')
        args = ('worktree', 'add', str(path), data['branch']) if refs else (
            'worktree', 'add', '-b', data['branch'], str(path), data['base_commit'])
        git(repo, *args)
    files(path)
    return path


def apply_changes(worktree, result, policy, *, protected_files):
    root = Path(worktree)
    # Validate the ENTIRE proposal before the first write. No partial unsafe proposal.
    for c in result['changes']:
        path = root / relative_path(c['path'])
        if not allowed(c['path'], policy['write_paths']) or not path.resolve().is_relative_to(root):
            raise FactoryError('scope_violation', 'Worker attempted a write outside authorized product paths')
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p.is_relative_to(root)):
            raise FactoryError('unsafe_path', 'Worker edit traverses a symlink')
        if path.is_dir() or any(p.is_file() for p in path.parents if p.is_relative_to(root)):
            raise FactoryError('application_conflict', 'Edit conflicts with an existing file/directory', details={'path': c['path']})
        if c['path'] in protected_files:
            expected = protected_files[c['path']]
            if c['operation'] != 'write' or hashlib.sha256(c['content'].encode()).hexdigest() != expected['sha256']:
                raise FactoryError('verification_weakened', 'A required test or harness file is frozen for this execution')
    for c in result['changes']:
        path = root / c['path']
        if c['operation'] == 'delete':
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Idempotent replay following a crash halfway through applying the saved response.
            path.write_text(c['content'])


def tree_object(worktree, index_path):
    # Private index; ignores .gitignore so untracked code cannot escape the evidence identity.
    inventory = files(worktree)
    env = {'GIT_INDEX_FILE': str(index_path)}
    git(worktree, 'read-tree', '--empty', env=env)
    entries = bytearray()
    for path, meta in inventory.items():
        blob = git(worktree, 'hash-object', '-w', '--no-filters', '--stdin', input=(Path(worktree) / path).read_bytes())
        entries.extend(f"{'100755' if meta['executable'] else '100644'} {blob}\t{path}\0".encode())
    git(worktree, 'update-index', '-z', '--index-info', input=bytes(entries), env=env)
    return git(worktree, 'write-tree', env=env)


def make_commit(worktree, tree, parent, identifier, timestamp):
    date = f'@{int(timestamp)} +0000'
    return git(worktree, 'commit-tree', tree, '-p', parent,
        input=f'Factory accepted slice execution {identifier}\n'.encode(),
        env={'GIT_AUTHOR_NAME': 'Software Factory', 'GIT_AUTHOR_EMAIL': 'factory@localhost',
             'GIT_COMMITTER_NAME': 'Software Factory', 'GIT_COMMITTER_EMAIL': 'factory@localhost',
             'GIT_AUTHOR_DATE': date, 'GIT_COMMITTER_DATE': date})


def publish_ref(worktree, branch, commit, parent):
    current = git(worktree, 'rev-parse', 'refs/heads/' + branch)
    if current not in (commit, parent):
        raise FactoryError('publication_conflict', 'Managed branch changed since publication intent')
    if current != commit:
        git(worktree, 'update-ref', 'refs/heads/' + branch, commit, parent)
    git(worktree, 'read-tree', commit)  # Managed index only; never overwrites any working file.


@contextmanager
def lock_accepted_ref(repo, commit):
    """Git's own prepared verify transaction holds its ref lock through DB commit.

    No ref is changed. EOF on controller death releases locks in the Git process.
    """
    process = subprocess.Popen(['/usr/bin/git', '-C', str(repo), 'update-ref', '--stdin'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
             'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_TERMINAL_PROMPT': '0'})
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        for command, expected in [('start\n', b'start: ok\n'),
                ('verify refs/heads/factory/accepted ' + commit + '\nprepare\n', b'prepare: ok\n')]:
            process.stdin.write(command.encode()); process.stdin.flush()
            if not selector.select(5) or process.stdout.readline() != expected:
                raise FactoryError('stale_evidence', 'Could not lock the exact accepted candidate for closure')
        yield
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()
        process.stdout.close(); process.stderr.close(); selector.close()

"""Build a fixed owned Herdr without installing anything into global paths.

Requires explicit workspace-local cargo/rustup/Zig and an already downloaded,
hash-verified ConPTY package. Fetches only the pinned public upstream source.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('workspace', 'cargo', 'cargo-home', 'rustup-home', 'zig', 'conpty-package', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('this fixed build targets Windows x64 MSVC')
    root = Path(__file__).resolve().parents[2]
    references = root / 'third_party/herdr-owned'
    manifest = json.loads((references / 'manifest.json').read_text(encoding='utf-8'))
    workspace, output = args.workspace.resolve(), args.output.resolve()
    if not args.workspace.is_absolute() or not args.output.is_absolute() or workspace == output:
        parser.error('workspace and output must be separate absolute paths')
    workspace.mkdir(parents=True, exist_ok=True)
    source = workspace / 'source'
    if source.exists() or output.exists():
        parser.error('source/output already exist; choose fresh paths (nothing is overwritten)')
    environment = os.environ.copy()
    environment.update({'CARGO_HOME': str(args.cargo_home.resolve()), 'RUSTUP_HOME': str(args.rustup_home.resolve()),
        'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': 'NUL', 'GIT_TERMINAL_PROMPT': '0',
        'CARGO_TARGET_DIR': str(workspace / 'target'), 'ZIG': str(args.zig.resolve()),
        'ZIG_GLOBAL_CACHE_DIR': str(workspace / 'zig-cache'), 'HERDR_BUILD_CHANNEL': 'meido-owned',
        'HERDR_BUILD_ID': 'meido-owned-1', 'HERDR_BUILD_COMMIT': manifest['commit'],
        'PATH': str(args.cargo.resolve().parent) + os.pathsep + os.environ.get('PATH', '')})
    def run(argv, cwd=workspace):
        if argv[0] == 'git':
            argv = ['git', '-c', 'core.hooksPath=NUL', '-c', 'credential.helper=', *argv[1:]]
        subprocess.run([str(item) for item in argv], cwd=cwd, env=environment, check=True)
    cargo_version = subprocess.check_output([str(args.cargo), '--version'], env=environment, text=True)
    zig_version = subprocess.check_output([str(args.zig), 'version'], env=environment, text=True).strip()
    if not cargo_version.startswith('cargo ' + manifest['rust'] + ' ') or zig_version != manifest['zig']:
        parser.error('explicit toolchains do not match pinned Rust/Zig versions')
    patch = references / 'owned.patch'
    if hashlib.sha256(patch.read_bytes()).hexdigest() != manifest['patchSha256']:
        parser.error('owned patch hash mismatch')
    run(['git', 'clone', '--no-checkout', manifest['upstream'], source])
    run(['git', 'checkout', '--detach', manifest['commit']], source)
    run(['git', 'apply', '--check', patch], source)
    run(['git', 'apply', patch], source)
    run([args.cargo, 'build', '--locked', '-j', '2'], source)
    if hashlib.sha256((workspace / 'target/debug/herdr.exe').read_bytes()).hexdigest() != manifest['acceptedBuildSha256']:
        parser.error('new binary differs from accepted build; repeat native acceptance and deliberately update the first-party lock before packaging')
    run([sys.executable, source / 'scripts/package_windows_conpty.py', 'stage', '--package', args.conpty_package.resolve(),
         '--herdr-exe', workspace / 'target/debug/herdr.exe', '--output-dir', output], source)
    notices = output / 'THIRD-PARTY-NOTICES'
    notices.mkdir(exist_ok=True)
    for relative in manifest['notices']:
        name = Path(relative).name
        shutil.copyfile(references / name, notices / name)
    files = {path.relative_to(output).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in output.rglob('*') if path.is_file()}
    (output / 'meido-owned-build.json').write_text(json.dumps(manifest | {'files': files}, indent=2) + '\n', encoding='utf-8')
    print('Fixed owned build staged at ' + str(output))


if __name__ == '__main__':
    main()

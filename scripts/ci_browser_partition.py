"""Run an exhaustive, disjoint part of the existing isolated browser suite."""
import argparse
import subprocess
import sys

from project import ROOT, browser_specs


def selected_specs(index, total):
    if type(index) is not int or type(total) is not int or not 1 <= total <= 16 or not 0 <= index < total:
        raise ValueError('Invalid browser partition')
    names = [path.name for path in browser_specs()]
    if len(names) < total:
        raise ValueError('Every browser partition must contain active tests')
    return names[index::total]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--index', type=int, required=True)
    parser.add_argument('--total', type=int, required=True)
    args = parser.parse_args()
    names = selected_specs(args.index, args.total)
    print(f'Browser partition {args.index + 1}/{args.total}: {len(names)} active files', flush=True)
    return subprocess.run([sys.executable, str(ROOT / 'scripts/project.py'), 'test',
                           '--suite', 'browser', '--spec', *names], cwd=ROOT).returncode


if __name__ == '__main__':
    raise SystemExit(main())

"""Private detached process entrypoint. Receives IDs, never shell commands."""
import argparse
from .application import FactoryService
from .registry import Registry


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    FactoryService(Registry(args.home)).run_pending(args.project, args.run_id)


if __name__ == '__main__':
    main()

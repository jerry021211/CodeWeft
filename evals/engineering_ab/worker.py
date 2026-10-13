import argparse
from pathlib import Path
from evals.cvm_verification.worker import execute
from evals.engineering_ab.runtime import Observer, configure, register
from evals.engineering_ab.offline import OfflineSDK

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--trial', type=Path, required=True)
    execute(parser.parse_args().trial, observer_type=Observer, offline_factory=OfflineSDK, configure=configure, register=register)

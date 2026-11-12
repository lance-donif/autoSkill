"""Load the exact pre-recovery solver without importing or changing product code."""
import hashlib
import importlib.machinery
import importlib.util
import sys
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

SOURCE = Path(__file__).with_name('frozen_captcha.py')
SOURCE_SHA256 = '18b899e023f29f85da0ab07dd3d10e389f38b85e6fc8ebe9233ecae0f59db6ea'
SOURCE_COMMIT = '336480a09753b43521b1f0fcd000014bc715e04d'


class FrozenSourceLoader(importlib.machinery.SourceFileLoader):
    """Verify the historical bytes and bypass any stale compiled cache."""

    def get_code(self, fullname):
        if fullname != self.name:
            raise ImportError('Historical solver module name mismatch')
        source = self.get_data(self.path)
        if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
            raise ImportError('The frozen pre-recovery solver source changed')
        return self.source_to_code(source, self.path)


def load_solver(product):
    """Create an isolated old solver; the checkout only supplies its model root.

    The frozen source uses just paths.SKILL. Supply that value in a temporary
    private package instead of importing the moving autoskill package. Each
    caller gets independent FOLDER/_loaded state, including in an existing REPL.
    """
    package = ModuleType('_autoskill_historical_' + uuid.uuid4().hex)
    package.__path__ = []
    package.paths = SimpleNamespace(SKILL=Path(product).resolve() / 'skills/autoskill')
    name = package.__name__ + '.captcha'
    loader = FrozenSourceLoader(name, str(SOURCE))
    spec = importlib.util.spec_from_file_location(name, SOURCE, loader=loader)
    solver = importlib.util.module_from_spec(spec)
    sys.modules[package.__name__] = package
    try:
        loader.exec_module(solver)
    finally:
        del sys.modules[package.__name__]
    solver.research_provenance = {
        'kind': 'frozen-pre-color-recovery-solver', 'source_commit': SOURCE_COMMIT,
        'source': str(SOURCE.resolve()), 'source_sha256': SOURCE_SHA256,
        'loader_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'current_product_solver_imported': False,
    }
    return solver

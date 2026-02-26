import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from furl import furl as Furl
from canonicalize import canonicalize, AnyHost, Host, Path, Rule, StripParams

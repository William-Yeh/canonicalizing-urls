import sys
sys.path.insert(0, "scripts")

from furl import furl as Furl
from canonicalize import canonicalize, AnyHost, Host, Path, Rule, StripParams

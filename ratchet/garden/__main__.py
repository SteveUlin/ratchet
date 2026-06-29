"""garden CLI dispatch — one package, two gardener phases. `python -m ratchet.garden …` tags (3b, the
default); `python -m ratchet.garden propose …` runs the structural-ops proposer + staleness pass (3c).
A git-style subcommand keeps the phase-1 (tagging) surface byte-identical to the pre-split module."""
import sys

from .propose import propose_main
from .tag import main

if len(sys.argv) > 1 and sys.argv[1] == "propose":
    propose_main(sys.argv[2:])
else:
    main()

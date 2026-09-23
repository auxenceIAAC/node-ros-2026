uint8 PASS=0
uint8 WARN=1
uint8 FAIL=2
uint8 SKIPPED=3

string id                  # stable identifier, e.g. "gnss.fix"
string name                # human name, e.g. "GNSS fix"
uint8  status
string message             # plain language, e.g. "4 satellites, 6 required"
string remedy              # what to DO, e.g. "wait, or clear the antenna view"
float64 measured_value
string  units
bool    active             # true if this test exercised hardware

# The setup field this check is waiting on, or empty.
#
# Empty for every check reporting a fault. Set only where the answer is
# "nobody has told us this yet", which needs a different response from the
# operator than "this is broken" — and which, named as a field rather than as
# a path to a file on the Jetson, is something they can actually go and do.
string  setup_field

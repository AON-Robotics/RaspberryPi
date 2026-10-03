#!/usr/bin/env bash
# Runs every test in this repo, plus the brain-side checks in the Override
# repo, and prints one PASS / FAIL / SKIP line per suite.
#
#   ./run_tests.sh             everything that can run on this machine
#   ./run_tests.sh --quick     skip the slow end-to-end pipeline tests (~2 min)
#   ./run_tests.sh --ollama    also drive the simulated robot with the local LLM
#   ./run_tests.sh --verbose   stream each suite's output instead of only logs
#
# Suites, in order:
#   1. setup        Python venv + pinned packages, build the brain simulator
#   2. cpp          this repo's C++ tests (ctest). On a Mac only the packet
#                   test can build; the serial/OTOS code needs Linux headers.
#   3. override     the brain code in ../Override (or $OVERRIDE_DIR):
#                     - portable protocol core, strict warnings, as the sim builds it
#                     - PROS glue checked against the PROS headers (host clang)
#                     - `pros make`, the real ARM build, if the PROS CLI is installed
#   4. bridge       Python unit tests: protocol, analysis, agent abort logic
#   5. pipeline     end to end: agent -> server -> serial (pty) -> brain_sim,
#                   fault injection, security layers, OTOS port sharing
#   6. llm          (--ollama) the real local model picks the right tools
#
# Logs: build/test-logs/<suite>.log. Exit code is non-zero if any suite failed.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
OVERRIDE_DIR="${OVERRIDE_DIR:-$ROOT/../Override}"
VENV="$ROOT/bridge/.venv"
PY="$VENV/bin/python"
LOGS="$ROOT/build/test-logs"
QUICK=0 OLLAMA=0 VERBOSE=0

for arg in "$@"; do
  case "$arg" in
    --quick) QUICK=1 ;;
    --ollama) OLLAMA=1 ;;
    --verbose) VERBOSE=1 ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)"; exit 2 ;;
  esac
done

mkdir -p "$LOGS"
RESULTS=()          # "STATUS|suite|detail|seconds"
FAILED=0

if [[ -t 1 ]]; then GREEN=$'\e[32m' RED=$'\e[31m' YELLOW=$'\e[33m' BOLD=$'\e[1m' RESET=$'\e[0m'
else GREEN="" RED="" YELLOW="" BOLD="" RESET=""; fi

record() {  # record STATUS suite detail seconds
  RESULTS+=("$1|$2|$3|$4")
  local color="$GREEN"
  [[ "$1" == FAIL ]] && color="$RED" && FAILED=1
  [[ "$1" == SKIP ]] && color="$YELLOW"
  printf '  %s%-4s%s  %-9s %s (%ss)\n' "$color" "$1" "$RESET" "$2" "$3" "$4"
}

# run_suite NAME DETAIL COMMAND... : runs the command, logs it, records the result.
run_suite() {
  local name="$1" detail="$2"; shift 2
  local log="$LOGS/$name.log" start=$SECONDS status=0
  printf '%s▶ %s%s: %s\n' "$BOLD" "$name" "$RESET" "$detail"
  if [[ $VERBOSE == 1 ]]; then
    "$@" 2>&1 | tee "$log"; status=${PIPESTATUS[0]}
  else
    "$@" >"$log" 2>&1; status=$?
  fi
  local summary
  summary="$(grep -E '[0-9]+ (passed|failed)|tests passed|errors=' "$log" | tail -1 || true)"
  if [[ $status == 0 ]]; then
    record PASS "$name" "${summary:-$detail}" $((SECONDS - start))
  else
    record FAIL "$name" "${summary:-exit code $status} - see $log" $((SECONDS - start))
    [[ $VERBOSE == 0 ]] && tail -25 "$log" | sed 's/^/      /'
  fi
}

skip() { printf '%s▶ %s%s\n' "$BOLD" "$1" "$RESET"; record SKIP "$1" "$2" 0; }

echo "${BOLD}Running all tests${RESET} (logs in ${LOGS#$ROOT/})"

# --- 1. setup -------------------------------------------------------------------
setup() (  # subshell: set -e and cd stay inside this suite
  set -e
  [[ -x "$PY" ]] || python3 -m venv "$VENV"
  "$PY" -m pip install -q --disable-pip-version-check \
    -r "$ROOT/bridge/server/requirements.txt" -r "$ROOT/bridge/web/requirements.txt" \
    -r "$ROOT/bridge/agent/requirements.txt" pytest
  cmake -S "$ROOT/sim" -B "$ROOT/sim/build" -DOVERRIDE_DIR="$OVERRIDE_DIR" >/dev/null
  cmake --build "$ROOT/sim/build"
)
if [[ -f "$OVERRIDE_DIR/src/aon/pi/protocol.cpp" ]]; then
  run_suite setup "venv + pinned packages, build sim/brain_sim" setup
else
  skip setup "Override not found at $OVERRIDE_DIR (set OVERRIDE_DIR); the simulator and pipeline need it"
  QUICK=1; OLLAMA=0
fi

# --- 2. this repo's C++ tests ---------------------------------------------------------
cpp_tests() (  # subshell: set -e and cd stay inside this suite
  set -e
  cmake -S "$ROOT" -B "$ROOT/build/cpp-tests" -DBUILD_TESTING=ON >/dev/null
  if [[ "$(uname)" == Linux ]]; then
    cmake --build "$ROOT/build/cpp-tests" -j4
    ctest --test-dir "$ROOT/build/cpp-tests" --output-on-failure
  else
    # otos.cpp needs <linux/i2c-dev.h>, so only the packet test builds here.
    cmake --build "$ROOT/build/cpp-tests" --target vexpi_packet_test
    ctest --test-dir "$ROOT/build/cpp-tests" -R packet_format --output-on-failure
  fi
)
if [[ "$(uname)" == Linux ]]; then
  run_suite cpp "ctest: packet format + serial recovery" cpp_tests
else
  run_suite cpp "ctest: packet format (serial recovery needs Linux: runs on the Pi)" cpp_tests
fi

# --- 3. brain code in Override ----------------------------------------------------------
override_checks() (  # subshell: set -e and cd stay inside this suite
  set -e
  cd "$OVERRIDE_DIR"
  local cxx="${CXX:-clang++}"
  echo "== portable core (no PROS), strict warnings"
  "$cxx" -std=c++17 -Wall -Wextra -Werror -fno-exceptions -fsyntax-only -Iinclude \
    src/aon/pi/protocol.cpp src/aon/pi/commands.cpp
  echo "== PROS glue + touched files against the PROS headers (host compiler stand-in)"
  local errors=0
  for f in src/aon/pi/pi-link.cpp src/main.cpp src/aon/odometry/odometry.cpp \
           src/aon/drivetrain/differential-drive.cpp; do
    if ! "$cxx" -std=gnu++20 -fsyntax-only -Iinclude -iquote include -D_USE_MATH_DEFINES -Wno-everything "$f"; then
      echo "FAILED: $f"; errors=$((errors + 1))
    fi
  done
  echo "errors=$errors"
  [[ $errors == 0 ]]
  if command -v pros >/dev/null; then
    echo "== pros make (real ARM build)"
    pros make
  else
    echo "== pros make: PROS CLI not installed, real ARM build not run"
  fi
)
if [[ -d "$OVERRIDE_DIR/src/aon/pi" ]]; then
  if command -v pros >/dev/null; then detail="core + PROS headers check + pros make"
  else detail="core + PROS headers check (no PROS CLI: pros make not run)"; fi
  run_suite override "$detail" override_checks
else
  skip override "Override repo not found at $OVERRIDE_DIR"
fi

# --- 4. bridge unit tests -----------------------------------------------------------------
if [[ -x "$PY" ]]; then
  run_suite bridge "protocol, odometry/diagnostic analysis, agent abort logic" \
    "$PY" -m pytest "$ROOT/bridge/tests" -q
else
  skip bridge "no Python venv (setup failed)"
fi

# --- 5. end-to-end pipeline ---------------------------------------------------------------
if [[ $QUICK == 1 ]]; then
  skip pipeline "--quick (or Override missing)"
elif [[ ! -x "$ROOT/sim/build/brain_sim" ]]; then
  skip pipeline "sim/build/brain_sim not built (setup failed)"
else
  run_suite pipeline "agent -> server -> serial -> brain_sim, faults, security, OTOS" \
    "$PY" -m pytest "$ROOT/sim/test_pipeline.py" -q -p no:cacheprovider
fi

# --- 6. real LLM --------------------------------------------------------------------------
if [[ $OLLAMA == 0 ]]; then
  skip llm "pass --ollama to drive the simulated robot with the local model"
elif ! curl -fs "${OLLAMA_URL:-http://127.0.0.1:11434}/api/version" >/dev/null; then
  skip llm "Ollama not reachable at ${OLLAMA_URL:-http://127.0.0.1:11434}"
else
  RUN_OLLAMA=1 run_suite llm "qwen3 picks move/turn/odometry_test/diagnose with the right arguments" \
    "$PY" -m pytest "$ROOT/sim/test_pipeline.py" -q -s -k real_llm -p no:cacheprovider
fi

# --- summary ----------------------------------------------------------------------------
echo
echo "${BOLD}Summary${RESET}"
for r in "${RESULTS[@]}"; do
  IFS='|' read -r status name detail secs <<<"$r"
  printf '  %-4s  %-9s %s\n' "$status" "$name" "$detail"
done
if [[ $FAILED == 0 ]]; then
  echo "${GREEN}${BOLD}All suites that ran passed.${RESET}"
else
  echo "${RED}${BOLD}Some suites failed; logs are in ${LOGS#$ROOT/}/.${RESET}"
fi
exit $FAILED

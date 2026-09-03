#!/bin/bash
# Sourced by run-all-control-tests.sh. Every test CRD is owned by one invocation.
AGENT_CRD_OWNER_LABEL=testing.kubescape.io/agent-crd-run
AGENT_CRD_RUN_ID=
AGENT_CRD_TEMP_DIR=
AGENT_CRD_CREATION_STARTED=false

cleanup_agent_test_crds() {
    local result=0
    if [[ "$AGENT_CRD_CREATION_STARTED" == true ]]; then
        # The ownership label also covers interruption during a create call.
        # Never delete a CRD just because its production name matches a fixture.
        kubectl delete customresourcedefinitions \
            --selector="$AGENT_CRD_OWNER_LABEL=$AGENT_CRD_RUN_ID" \
            --ignore-not-found || result=1
    fi
    if [[ -n "$AGENT_CRD_TEMP_DIR" ]]; then
        rm -rf -- "$AGENT_CRD_TEMP_DIR"
    fi
    return "$result"
}

setup_agent_test_crds() {
    AGENT_CRD_TEMP_DIR=$(mktemp -d) || return 1
    trap 'crd_exit_status=$?; cleanup_agent_test_crds || crd_exit_status=1; exit "$crd_exit_status"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM

    AGENT_CRD_RUN_ID=$("$PYTHON_EXECUTABLE" -c 'import uuid; print(uuid.uuid4())') || return 1
    "$PYTHON_EXECUTABLE" - "$1" "$AGENT_CRD_TEMP_DIR" "$AGENT_CRD_OWNER_LABEL" "$AGENT_CRD_RUN_ID" <<'PY'
import pathlib
import sys
import yaml

source, directory, label, owner = sys.argv[1:]
seen = set()
for index, crd in enumerate(yaml.safe_load_all(pathlib.Path(source).read_text())):
    if crd.get("kind") != "CustomResourceDefinition":
        raise ValueError("Agent test fixtures must contain only CRDs")
    name = crd["metadata"]["name"]
    if name in seen:
        raise ValueError("Duplicate test CRD: " + name)
    seen.add(name)
    crd["metadata"].setdefault("labels", {})[label] = owner
    pathlib.Path(directory, str(index) + ".yaml").write_text(yaml.safe_dump(crd))
    with pathlib.Path(directory, "names").open("a") as names:
        names.write(name + "\n")
if not seen:
    raise ValueError("No agent test CRDs found")
PY
    if [[ $? -ne 0 ]]; then return 1; fi

    # Check all names before creating anything. An API/auth failure is fatal too.
    local name existing
    while IFS= read -r name; do
        existing=$(kubectl get customresourcedefinition "$name" --ignore-not-found -o name) || return 1
        if [[ -n "$existing" ]]; then
            echo "Refusing to run: test CRD $name already exists. Use a disposable cluster." >&2
            return 1
        fi
    done < "$AGENT_CRD_TEMP_DIR/names"

    local manifest
    AGENT_CRD_CREATION_STARTED=true
    for manifest in "$AGENT_CRD_TEMP_DIR"/*.yaml; do
        # create (never apply) also protects a CRD installed after the preflight.
        kubectl create -f "$manifest" || return 1
    done
    while IFS= read -r name; do
        kubectl wait --for=condition=Established "customresourcedefinition/$name" --timeout=60s || return 1
    done < "$AGENT_CRD_TEMP_DIR/names"
}

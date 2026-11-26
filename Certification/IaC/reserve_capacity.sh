#!/usr/bin/env bash
# Reserve On-Demand capacity for the whole worker cluster in one Availability Zone, all or nothing.
#
# For each AZ in turn, one open On-Demand Capacity Reservation is created per instance type, with the
# counts from ../config.json ("nodes"), except LATER_TYPES (default g6e.xlarge). If any type fails, every reservation of this script in that AZ
# is cancelled right away and the next AZ is tried. Rounds repeat until one AZ holds them all.
# Then LATER_TYPES are reserved in that AZ, retried every FILL_INTERVAL seconds (default 10) without
# cancelling the others, until they are reserved too. LATER_TYPES="" reserves every type all or nothing.
# Once that happens, `terraform apply` with that AZ launches the workers into the reservations
# automatically (instances use matching open reservations by default).
#
#   ./reserve_capacity.sh                          # try us-west-2a, b, c every 60 s until one succeeds
#   ./reserve_capacity.sh us-west-2b us-west-2c    # try only these AZs, in this order
#   INTERVAL=120 ./reserve_capacity.sh             # wait 120 s between rounds
#   ./reserve_capacity.sh --once                   # one round only
#   ./reserve_capacity.sh --dry-run                # check permissions and parameters, reserve nothing
#   ./reserve_capacity.sh --list                   # show the reservations this script made
#   ./reserve_capacity.sh --cancel                 # cancel all of them
#   ./reserve_capacity.sh --test-notify            # show the success notification once
#
# On success (or when it stops on an error) it shows a desktop notification with a sound
# (macOS; notify-send on Linux). NOTIFY=0 turns this off.
#
# Billing: a reservation is charged at the On-Demand rate from the moment it is created until it is
# cancelled, whether or not an instance runs in it (an instance running in it is not charged twice).
# A failed attempt costs nothing. `terraform destroy` does NOT cancel these reservations.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TFVARS="$HERE/terraform.tfvars"
CONFIG="${CERT_CONFIG:-$HERE/../config.json}"

tfvar() {  # read a simple string variable from terraform.tfvars (never prints the file)
    [ -f "$TFVARS" ] && sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$TFVARS" | head -1
}

export AWS_PROFILE="${AWS_PROFILE:-$(tfvar awscli_profile)}"
export AWS_REGION="${AWS_REGION:-$(tfvar region)}"
AWS_REGION="${AWS_REGION:-us-west-2}"
[ -z "$AWS_PROFILE" ] && unset AWS_PROFILE
PREFIX="$(tfvar prefix)"
PREFIX="${PREFIX:-shuntserve-certification}"
TAG_KEY="ShuntCertReservation"   # value = prefix; used to find this script's reservations
INTERVAL="${INTERVAL:-60}"
LATER_TYPES="${LATER_TYPES-g6e.xlarge}"   # reserved after the others succeed in an AZ; set but empty = none
FILL_INTERVAL="${FILL_INTERVAL:-10}"

MODE="loop"
AZS=()
for arg in "$@"; do
    case "$arg" in
        --once)        MODE="once" ;;
        --dry-run)     MODE="dry-run" ;;
        --list)        MODE="list" ;;
        --cancel)      MODE="cancel" ;;
        --test-notify) MODE="test-notify" ;;
        -h|--help)     awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
        -*)            echo "Unknown option: $arg" >&2; exit 1 ;;
        *)             AZS+=("$arg") ;;
    esac
done
[ ${#AZS[@]} -eq 0 ] && AZS=("${AWS_REGION}a" "${AWS_REGION}b" "${AWS_REGION}c")

now() { date '+%H:%M:%S'; }

notify() {  # notify <title> <message>: desktop notification and sound (message must not contain double quotes)
    [ "${NOTIFY:-1}" = "0" ] && return 0
    printf '\a'
    if command -v osascript >/dev/null 2>&1; then
        osascript -e "display notification \"$2\" with title \"$1\" sound name \"Glass\"" >/dev/null 2>&1
        ( for _ in 1 2 3; do afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1; sleep 0.3; done ) &
    elif command -v notify-send >/dev/null 2>&1; then
        notify-send "$1" "$2" >/dev/null 2>&1
    fi
}

list_ids() {  # list_ids [az]: ids of this script's active or pending reservations (in one AZ if given)
    local filters=("Name=tag:$TAG_KEY,Values=$PREFIX" "Name=state,Values=active,pending")
    [ -n "${1:-}" ] && filters+=("Name=availability-zone,Values=$1")
    aws ec2 describe-capacity-reservations --filters "${filters[@]}" \
        --query 'CapacityReservations[].CapacityReservationId' --output text
}

cancel_ids() {  # cancel the given reservation ids; returns nonzero if any is still not cancelled
    local id state rc=0
    for id in "$@"; do
        if aws ec2 cancel-capacity-reservation --capacity-reservation-id "$id" >/dev/null 2>&1; then
            echo "  cancelled $id"
            continue
        fi
        state=$(aws ec2 describe-capacity-reservations --capacity-reservation-ids "$id" \
            --query 'CapacityReservations[0].State' --output text 2>/dev/null)
        case "$state" in
            cancelled|expired|failed) echo "  $id is already $state" ;;
            *) echo "  FAILED to cancel $id (state: ${state:-unknown}). It is still billed. Run --cancel again or cancel it in the console." >&2
               rc=1 ;;
        esac
    done
    return $rc
}

# ---- list / cancel / test-notify ---------------------------------------------------------------
if [ "$MODE" = "list" ]; then
    ids=$(list_ids) || exit 1
    if [ -z "$ids" ]; then echo "No active reservations made by this script."; exit 0; fi
    aws ec2 describe-capacity-reservations \
        --filters "Name=tag:$TAG_KEY,Values=$PREFIX" "Name=state,Values=active,pending" \
        --query 'CapacityReservations[].[CapacityReservationId,InstanceType,AvailabilityZone,TotalInstanceCount,AvailableInstanceCount,State,CreateDate]' \
        --output table
    echo "(TotalInstanceCount = reserved, AvailableInstanceCount = not yet used by a running instance)"
    exit 0
fi
if [ "$MODE" = "cancel" ]; then
    ids=$(list_ids) || exit 1
    if [ -z "$ids" ]; then echo "No active reservations made by this script."; exit 0; fi
    cancel_ids $ids
    exit $?
fi
if [ "$MODE" = "test-notify" ]; then
    notify "Capacity reserved (test)" "This is how a successful reservation is announced."
    echo "Sent a test notification."
    exit 0
fi

# ---- cluster shape from config.json ------------------------------------------------------------
TYPES=(); COUNTS=(); LTYPES=(); LCOUNTS=()
while read -r t n; do
    case " $LATER_TYPES " in
        *" $t "*) LTYPES+=("$t"); LCOUNTS+=("$n") ;;
        *)        TYPES+=("$t"); COUNTS+=("$n") ;;
    esac
done < <(
    python3 -c 'import json,sys; [print(t, len(ips)) for t, ips in json.load(open(sys.argv[1]))["nodes"].items()]' "$CONFIG")
if [ ${#TYPES[@]} -eq 0 ]; then echo "Could not read the cluster shape from $CONFIG" >&2; exit 1; fi

shape=""
for i in "${!TYPES[@]}"; do shape+="${TYPES[$i]} x${COUNTS[$i]}, "; done
echo "Cluster : ${shape%, }"
lshape=""
for i in ${LTYPES[@]+"${!LTYPES[@]}"}; do lshape+="${LTYPES[$i]} x${LCOUNTS[$i]}, "; done
[ -n "$lshape" ] && echo "Then    : ${lshape%, } (in the AZ that got the above, retried until reserved)"
echo "AZs     : ${AZS[*]}"
echo "Profile : ${AWS_PROFILE:-default}   Region: $AWS_REGION"

client_token() {  # unique per request: if the CLI resends a request whose reply was lost, AWS returns the
                  # reservation it already made instead of creating a second one
    uuidgen 2>/dev/null || echo "$$-$(date +%s)-$RANDOM$RANDOM"
}

reserve() {  # reserve <type> <count> <az> [--dry-run] -> prints "id state count" or the AWS error
    aws ec2 create-capacity-reservation \
        --instance-type "$1" --instance-count "$2" --availability-zone "$3" \
        --instance-platform Linux/UNIX --tenancy default \
        --instance-match-criteria open --end-date-type unlimited \
        --client-token "$(client_token)" \
        --tag-specifications "ResourceType=capacity-reservation,Tags=[{Key=Name,Value=${PREFIX}-shunt-cert-reservation},{Key=$TAG_KEY,Value=$PREFIX}]" \
        ${4:-} --query 'CapacityReservation.[CapacityReservationId,State,TotalInstanceCount]' --output text 2>&1
}

error_code() { sed -n 's/.*An error occurred (\([^)]*\)).*/\1/p' <<<"$1" | head -1; }

# ---- dry run -----------------------------------------------------------------------------------
if [ "$MODE" = "dry-run" ]; then
    echo "Dry run: checks permissions and parameters only. Capacity is not checked, nothing is reserved."
    rc=0
    ALL_T=("${TYPES[@]}" ${LTYPES[@]+"${LTYPES[@]}"}); ALL_C=("${COUNTS[@]}" ${LCOUNTS[@]+"${LCOUNTS[@]}"})
    for az in "${AZS[@]}"; do
        for i in "${!ALL_T[@]}"; do
            out=$(reserve "${ALL_T[$i]}" "${ALL_C[$i]}" "$az" --dry-run)
            if [ "$(error_code "$out")" = "DryRunOperation" ]; then
                printf "  %-11s %-12s x%-2s would be accepted\n" "$az" "${ALL_T[$i]}" "${ALL_C[$i]}"
            else
                printf "  %-11s %-12s x%-2s %s\n" "$az" "${ALL_T[$i]}" "${ALL_C[$i]}" "$out"; rc=1
            fi
        done
    done
    exit $rc
fi

# ---- reserve -----------------------------------------------------------------------------------
existing=$(list_ids) || exit 1
if [ -n "$existing" ]; then
    echo "This script already has active reservations: $existing" >&2
    echo "Check them with --list. Cancel them with --cancel before reserving again." >&2
    exit 1
fi

HELD=()          # reservations made in the AZ currently being tried
CURRENT_AZ=""    # AZ being tried right now (released on failure or Ctrl-C)

release_az() {  # release_az <az>: cancel what this attempt reserved there, then anything else of ours there
    local az="$1" id others=()
    [ ${#HELD[@]} -gt 0 ] && cancel_ids "${HELD[@]}" >/dev/null
    for id in $(list_ids "$az" 2>/dev/null); do
        case " ${HELD[*]:-} " in *" $id "*) ;; *) others+=("$id") ;; esac
    done
    if [ ${#others[@]} -gt 0 ]; then
        echo "  also cancelling ${#others[@]} other reservation(s) of this script in $az"
        cancel_ids "${others[@]}"
    fi
    HELD=()
}

on_interrupt() {
    echo
    if [ -n "$CURRENT_AZ" ]; then
        echo "Interrupted. Releasing this script's reservations in $CURRENT_AZ."
        release_az "$CURRENT_AZ"
    fi
    exit 130
}
trap on_interrupt INT TERM

try_az() {  # try_az <az> -> 0 if the whole cluster is reserved there (ids left in HELD), 1 otherwise; 2 = fatal
    local az="$1" i out code id state total line="" waited
    HELD=()
    CURRENT_AZ="$az"
    for i in "${!TYPES[@]}"; do
        out=$(reserve "${TYPES[$i]}" "${COUNTS[$i]}" "$az")
        code=$(error_code "$out")
        if [ -z "$code" ]; then
            read -r id state total <<<"$out"
            HELD+=("$id")
            waited=0
            while [ "$state" = "pending" ] && [ $waited -lt 60 ]; do
                sleep 2; waited=$((waited + 2))
                state=$(aws ec2 describe-capacity-reservations --capacity-reservation-ids "$id" \
                    --query 'CapacityReservations[0].State' --output text 2>/dev/null)
            done
            if [ "$state" = "active" ] && [ "$total" = "${COUNTS[$i]}" ]; then
                line+="${TYPES[$i]} x${COUNTS[$i]} ok, "
                continue
            fi
            line+="${TYPES[$i]} x${COUNTS[$i]} not usable (state $state, $total reserved), "
        else
            case "$code" in
                Insufficient*)  line+="${TYPES[$i]} x${COUNTS[$i]} no capacity, " ;;
                Unsupported)    line+="${TYPES[$i]} not offered in this AZ, " ;;
                RequestLimitExceeded|Throttling|InternalError|Unavailable|ServiceUnavailable)
                                line+="${TYPES[$i]} x${COUNTS[$i]} AWS busy ($code), " ;;
                *)              echo "[$(now)] $az: ${TYPES[$i]} x${COUNTS[$i]} failed: $out" >&2
                                release_az "$az"
                                CURRENT_AZ=""
                                return 2 ;;
            esac
        fi
        # This AZ cannot hold the whole cluster: release this script's reservations there.
        [ ${#HELD[@]} -gt 0 ] && line+="released ${#HELD[@]} reservation(s), "
        echo "[$(now)] round $ROUND  $az: ${line%, }"
        release_az "$az"
        CURRENT_AZ=""
        return 1
    done
    echo "[$(now)] round $ROUND  $az: ${line%, }"
    return 0
}

fill_later() {  # fill_later <az>: reserve each LATER type there, retrying until it succeeds; 0 = done, 2 = fatal
    local az="$1" i out code id state total waited attempt
    for i in "${!LTYPES[@]}"; do
        attempt=0
        while :; do
            attempt=$((attempt + 1))
            out=$(reserve "${LTYPES[$i]}" "${LCOUNTS[$i]}" "$az")
            code=$(error_code "$out")
            if [ -z "$code" ]; then
                read -r id state total <<<"$out"
                HELD+=("$id")
                waited=0
                while [ "$state" = "pending" ] && [ $waited -lt 60 ]; do
                    sleep 2; waited=$((waited + 2))
                    state=$(aws ec2 describe-capacity-reservations --capacity-reservation-ids "$id" \
                        --query 'CapacityReservations[0].State' --output text 2>/dev/null)
                done
                if [ "$state" = "active" ] && [ "$total" = "${LCOUNTS[$i]}" ]; then
                    echo "[$(now)] $az: ${LTYPES[$i]} x${LCOUNTS[$i]} ok (attempt $attempt)"
                    break
                fi
                echo "[$(now)] $az: ${LTYPES[$i]} reservation $id not usable (state $state, $total reserved), cancelling it"
                cancel_ids "$id" >/dev/null
            else
                case "$code" in
                    Insufficient*|RequestLimitExceeded|Throttling|InternalError|Unavailable|ServiceUnavailable) ;;
                    *) echo "[$(now)] $az: ${LTYPES[$i]} x${LCOUNTS[$i]} failed: $out" >&2
                       release_az "$az"
                       CURRENT_AZ=""
                       return 2 ;;
                esac
            fi
            [ $((attempt % 6)) -eq 0 ] && echo "[$(now)] $az: ${LTYPES[$i]} x${LCOUNTS[$i]} still no capacity ($attempt attempts)"
            sleep "$FILL_INTERVAL"
        done
    done
    return 0
}

ROUND=1
while :; do
    for az in "${AZS[@]}"; do
        try_az "$az"; rc=$?
        if [ $rc -eq 0 ] && [ -n "$lshape" ]; then
            echo "Reserved ${shape%, } in $az. Billing has started. Now trying ${lshape%, } there every ${FILL_INTERVAL}s until it is reserved too."
            echo "(Ctrl-C cancels everything reserved in $az.)"
            notify "Reserved ${shape%, } in $az" "Billing has started. Now retrying ${lshape%, } in $az."
            fill_later "$az"; rc=$?
        fi
        if [ $rc -eq 2 ]; then
            echo "Stopped: the error above is not a capacity shortage." >&2
            notify "Capacity reservation stopped" "Stopped on an error that is not a capacity shortage. See the terminal."
            exit 1
        fi
        if [ $rc -eq 0 ]; then
            trap - INT TERM
            echo
            echo "Reserved the cluster in $az. Billing for these reservations has started (On-Demand rate):"
            aws ec2 describe-capacity-reservations --filters "Name=tag:$TAG_KEY,Values=$PREFIX" "Name=state,Values=active" "Name=availability-zone,Values=$az" \
                --query 'CapacityReservations[].[InstanceType,TotalInstanceCount,CapacityReservationId]' --output text | sed 's/^/  /'
            current=$(tfvar availability_zone)
            echo
            echo "Next:"
            if [ "$current" != "$az" ]; then
                echo "  1. In terraform.tfvars set:  availability_zone = \"$az\"   (now: ${current:-not set, default us-west-2a})"
            else
                echo "  1. terraform.tfvars already has availability_zone = \"$az\""
            fi
            echo "  2. terraform apply   (the workers launch into these reservations)"
            echo "  3. When every worker is running: ./reserve_capacity.sh --cancel"
            echo "     The running workers keep running. Only the reservations go away, so they cannot be forgotten and billed later."
            notify "Capacity reserved in $az" "Billing has started. Set the AZ in terraform.tfvars and run terraform apply."
            exit 0
        fi
    done
    [ "$MODE" = "once" ] && { echo "No AZ could hold the whole cluster this round."; exit 1; }
    sleep "$INTERVAL"
    ROUND=$((ROUND + 1))
done

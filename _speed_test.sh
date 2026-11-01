#!/bin/bash
# Test max speed break: coba delay mulai cepat, deteksi reject
test_break() {
  local delay=$1
  local tx=$2
  local ty=$3
  local maxhits=$3
  local pre_rej pre_ack
  pre_rej=$(grep -c "block_reject" stay_world.log)
  pre_ack=$(grep -c "block_ack" stay_world.log)
  curl -s -X POST http://127.0.0.1:8787/bots/default/break -H 'Content-Type: application/json' \
    -d "{\"x\":$tx,\"y\":$ty,\"delay\":$delay,\"max\":$maxhits}" > /dev/null
  # tunggu sesuai total waktu hit
  local secs=$(python3 -c "print(max(1, int($delay * $maxhits / 1000 * 1000 + 2)))" 2>/dev/null || echo 4)
  sleep $secs
  local post_rej post_ack
  post_rej=$(grep -c "block_reject" stay_world.log)
  post_ack=$(grep -c "block_ack" stay_world.log)
  local rej_delta=$((post_rej - pre_rej))
  local ack_delta=$((post_ack - pre_ack))
  local result="OK"
  [ $rej_delta -gt 0 ] && result="REJECT($rej_delta)"
  echo "delay=${delay}s tile=($tx,$ty) max=$maxhits -> reject+$rej_delta ack+$ack_delta : $result"
}

# Bot di (84,24), DIRT di (84,25) dist=1
echo "=== TEST BREAK SPEED ==="
# Test dari cepat ke lambat
test_break 0.005 84 25 30
test_break 0.01  84 25 30
test_break 0.02  84 25 30
test_break 0.03  84 25 30
test_break 0.05  84 25 30
test_break 0.08  84 25 30
test_break 0.16  84 25 30
test_break 0.2   84 25 30

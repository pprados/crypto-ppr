#!/usr/bin/env bash
# Coupe et relance le Wifi régulièrement pour s'assurer de la résiliance
# Et injecte des erreurs sur le réseau
function cleanup {
  sudo tc qdisc del dev wlp0s20f3 root netem
}

trap cleanup EXIT
sudo tc qdisc add dev wlp0s20f3 root netem delay 10ms reorder 25% 50% corrupt 1%

MIN=5
MAX=30
while :
do
  nmcli radio wifi off
  echo WIFI off
  z=$[ ( $RANDOM % ($MAX - $MIN) ) + $MIN ]s
  echo sleep $z
  sleep "$z"
  nmcli radio wifi on
  echo WIFI on
  z=$[ ( $RANDOM % ($MAX - $MIN) ) + $MIN ]s
  echo sleep $z
  sleep "$z"
done
#!/bin/bash

ROOT=$HOME/syndicate/syndicate-UG-root
NAME="syndicate-UG"
VERSION="0.$(date +%Y\%m\%d\%H\%M\%S)"

DEPS="libssl1.0.0 libcurl3-gnutls libprotobuf7 libsyndicate fuse libboost-system1.48.0 libboost-thread1.48.0" 

DEPARGS=""
for pkg in $DEPS; do
   DEPARGS="$DEPARGS -d $pkg"
done

source /usr/local/rvm/scripts/rvm

rm -f $NAME-0*.deb

fpm --force -s dir -t deb -a $(uname -p) -v $VERSION -n $NAME $DEPARGS -C $ROOT --license "Apache 2.0" --vendor "Princeton University" --description "Syndicate User Gateway binaries.  Includes syndicatefs, syndicate-httpd, syndicate-ipc." $(ls $ROOT)

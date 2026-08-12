#!/usr/bin/env bash

command -v jsreport || npm install @jsreport/jsreport-cli -g
jsreport init
jsreport configure

echo Run 'jsreport start'

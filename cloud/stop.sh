#!/bin/bash
launchctl bootout "gui/$(id -u)/com.pb.telegram.assistant" 2>/dev/null || true
echo 'Automatic service stopped; journal and token preserved.'

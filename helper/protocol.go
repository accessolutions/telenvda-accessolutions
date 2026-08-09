// Line based protocol spoken with the TeleNVDA add-on.
//
// One JSON object per line travels in each direction: commands on the standard
// input, events on the standard output. Nothing else may ever be written to the
// standard output, since a stray line would be read as an event; diagnostics go
// to the standard error, which the add-on discards.
package main

import (
	"encoding/json"
	"os"
	"sync"
)

// Roles this program may be started with.
const (
	rolePublisher = "publisher"
	roleViewer    = "viewer"
)

// command is a request coming from the add-on. A single structure covers every
// command, the unused fields simply staying at their zero value.
type command struct {
	Command    string      `json:"command"`
	Role       string      `json:"role"`
	AllowInput bool        `json:"allow_input"`
	IceServers []iceServer `json:"ice_servers"`
	MaxFps     int         `json:"max_fps"`
	Quality    string      `json:"quality"`
	SDP        string      `json:"sdp"`
	Candidate  string      `json:"candidate"`
}

// iceServer mirrors the description the relay hands out, which follows the
// WebRTC one so that it can be used as is.
type iceServer struct {
	Urls       []string `json:"urls"`
	Username   string   `json:"username"`
	Credential string   `json:"credential"`
}

// event is a notification sent back to the add-on.
type event struct {
	Event     string `json:"event"`
	SDP       string `json:"sdp,omitempty"`
	Candidate string `json:"candidate,omitempty"`
	Reason    string `json:"reason,omitempty"`
}

// inputEvent describes a mouse action the viewer asks the publisher to replay.
// Coordinates are given as a fraction of the shared picture, so that they stay
// meaningful whatever resolution each end works at.
type inputEvent struct {
	Type  string  `json:"t"`
	X     float64 `json:"x"`
	Y     float64 `json:"y"`
	Btn   string  `json:"b,omitempty"`
	Delta int     `json:"d,omitempty"`
}

var emitMutex sync.Mutex

// emit writes one event, atomically with respect to the other goroutines.
func emit(e event) {
	data, err := json.Marshal(e)
	if err != nil {
		return
	}
	emitMutex.Lock()
	defer emitMutex.Unlock()
	_, _ = os.Stdout.Write(append(data, '\n'))
}

func emitFailure(reason string) {
	emit(event{Event: "failed", Reason: reason})
}

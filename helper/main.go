// Screen sharing helper for the TeleNVDA add-on.
//
// The add-on cannot host a WebRTC stack itself: the screen reader ships several
// incompatible Python builds, and compressing a video stream in the same process
// as the speech would starve it. This program does that work outside, and is
// driven with a line based protocol so that it can be replaced or rewritten
// without touching the add-on.
//
// It has no interface of its own beyond the window showing the shared screen,
// and it exits as soon as the add-on closes its standard input, so that no
// session can outlive the add-on that started it.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"sync"
)

// maxCommandSize bounds a line read from the add-on. Session descriptions are
// the longest thing sent and stay far below this.
const maxCommandSize = 1 << 20

var stderr = os.Stderr

func main() {
	role := flag.String("role", "", "publisher to share this screen, viewer to display the other one")
	flag.Parse()
	if *role != rolePublisher && *role != roleViewer {
		fmt.Fprintln(stderr, "the role parameter must be publisher or viewer")
		os.Exit(2)
	}
	// Without this the graphics interface reports and captures a scaled down
	// desktop on high resolution displays.
	procSetProcessDPIAware.Call()

	quit := make(chan struct{})
	var once sync.Once
	shutdown := func() { once.Do(func() { close(quit) }) }

	s := newSession(shutdown)
	go func() {
		readCommands(s, *role)
		// The add-on closed its end, so there is nothing left to serve.
		s.stop()
	}()

	<-quit
}

// readCommands handles the add-on requests until its standard input is closed.
func readCommands(s *session, role string) {
	reader := bufio.NewReaderSize(os.Stdin, 64*1024)
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 0, 64*1024), maxCommandSize)
	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var cmd command
		if err := json.Unmarshal(line, &cmd); err != nil {
			logf("an unreadable command was discarded: %v", err)
			continue
		}
		if !dispatch(s, role, cmd) {
			return
		}
	}
	if err := scanner.Err(); err != nil {
		logf("the commands could not be read: %v", err)
	}
}

// dispatch carries out one command and reports whether reading should go on.
func dispatch(s *session, role string, cmd command) bool {
	switch cmd.Command {
	case "start":
		// The role given on the command line is the one that counts, so that a
		// stray command cannot turn a viewer into a publisher.
		cmd.Role = role
		if err := s.start(cmd); err != nil {
			logf("the session could not be started: %v", err)
			s.fail("start_failed")
			return false
		}
	case "offer":
		if err := s.handleOffer(cmd.SDP); err != nil {
			logf("the offer was refused: %v", err)
			s.fail("bad_offer")
			return false
		}
	case "answer":
		if err := s.handleAnswer(cmd.SDP); err != nil {
			logf("the answer was refused: %v", err)
			s.fail("bad_answer")
			return false
		}
	case "candidate":
		if err := s.handleCandidate(cmd.Candidate); err != nil {
			// A single unusable route is not worth ending a session for, since
			// the others may still lead somewhere.
			logf("a candidate was discarded: %v", err)
		}
	case "stop":
		s.stop()
		return false
	default:
		logf("unknown command %q", cmd.Command)
	}
	return true
}

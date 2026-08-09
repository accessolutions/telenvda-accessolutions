//go:build amd64 || arm64

package main

// mouseInput is the INPUT structure restricted to its mouse variant.
//
// On sixty four bit builds the union that follows the kind of input contains a
// pointer sized field, so it starts on an eight byte boundary and four bytes of
// padding sit after the kind.
type mouseInput struct {
	Type      uint32
	_         uint32
	Dx        int32
	Dy        int32
	MouseData uint32
	Flags     uint32
	Time      uint32
	ExtraInfo uintptr
}

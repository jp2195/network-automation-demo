package main

import (
	"fmt"
	"io"
	"log"
	"os"
)

// printer is the shared closure all YAML/config emitters use. Calling
// p("apiVersion: v1") writes "apiVersion: v1\n" to the underlying writer.
type printer func(format string, args ...interface{})

// newPrinter returns a printer that writes to w, appending "\n" to each
// call so emitters don't have to embed \n in their format strings.
func newPrinter(w io.Writer) printer {
	return func(format string, args ...interface{}) {
		fmt.Fprintf(w, format+"\n", args...)
	}
}

// renderTo opens path for writing, calls emit(f), closes the file, and
// prints a one-line progress message. Exits via log.Fatal on any error.
func renderTo(path, label string, emit func(io.Writer) error) {
	if label != "" {
		fmt.Printf("==> Writing %s%s\n", path, label)
	} else {
		fmt.Printf("==> Writing %s\n", path)
	}
	f, err := os.Create(path)
	if err != nil {
		log.Fatal(err)
	}
	defer f.Close()
	if err := emit(f); err != nil {
		log.Fatal(err)
	}
}

# GDB Python API Reference

> Scraped from https://sourceware.org/gdb/current/onlinedocs/gdb.html/Python-API.html (GDB current onlinedocs).

## Table of Contents

- [23.3.1 Python Commands](#2331-python-commands)
- [23.3.2 Python API](#2332-python-api)
- [23.3.2.1 Basic Python](#23321-basic-python)
- [23.3.2.2 Threading in GDB](#23322-threading-in-gdb)
- [23.3.2.3 Exception Handling](#23323-exception-handling)
- [23.3.2.4 Values From Inferior](#23324-values-from-inferior)
- [23.3.2.5 Types In Python](#23325-types-in-python)
- [23.3.2.6 Pretty Printing API](#23326-pretty-printing-api)
- [23.3.2.7 Selecting Pretty-Printers](#23327-selecting-pretty-printers)
- [23.3.2.8 Writing a Pretty-Printer](#23328-writing-a-pretty-printer)
- [23.3.2.9 Type Printing API](#23329-type-printing-api)
- [23.3.2.10 Filtering Frames](#233210-filtering-frames)
- [23.3.2.11 Decorating Frames](#233211-decorating-frames)
- [23.3.2.12 Writing a Frame Filter](#233212-writing-a-frame-filter)
- [23.3.2.13 Unwinding Frames in Python](#233213-unwinding-frames-in-python)
- [23.3.2.14 Xmethods In Python](#233214-xmethods-in-python)
- [23.3.2.15 Xmethod API](#233215-xmethod-api)
- [23.3.2.16 Writing an Xmethod](#233216-writing-an-xmethod)
- [23.3.2.17 Inferiors In Python](#233217-inferiors-in-python)
- [23.3.2.18 Events In Python](#233218-events-in-python)
- [23.3.2.19 Threads In Python](#233219-threads-in-python)
- [23.3.2.20 Recordings In Python](#233220-recordings-in-python)
- [23.3.2.21 CLI Commands In Python](#233221-cli-commands-in-python)
- [23.3.2.22 GDB/MI Commands In Python](#233222-gdb/mi-commands-in-python)
- [23.3.2.23 GDB/MI Notifications In Python](#233223-gdb/mi-notifications-in-python)
- [23.3.2.24 Parameters In Python](#233224-parameters-in-python)
- [23.3.2.25 Writing new convenience functions](#233225-writing-new-convenience-functions)
- [23.3.2.26 Program Spaces In Python](#233226-program-spaces-in-python)
- [23.3.2.27 Objfiles In Python](#233227-objfiles-in-python)
- [23.3.2.28 Accessing inferior stack frames from Python](#233228-accessing-inferior-stack-frames-from-python)
- [23.3.2.29 Accessing blocks from Python](#233229-accessing-blocks-from-python)
- [23.3.2.30 Python representation of Symbols](#233230-python-representation-of-symbols)
- [23.3.2.31 Symbol table representation in Python](#233231-symbol-table-representation-in-python)
- [23.3.2.32 Manipulating line tables using Python](#233232-manipulating-line-tables-using-python)
- [23.3.2.33 Manipulating breakpoints using Python](#233233-manipulating-breakpoints-using-python)
- [23.3.2.34 Finish Breakpoints](#233234-finish-breakpoints)
- [23.3.2.35 Python representation of lazy strings](#233235-python-representation-of-lazy-strings)
- [23.3.2.36 Python representation of colors](#233236-python-representation-of-colors)
- [23.3.2.37 Python representation of styles](#233237-python-representation-of-styles)
- [23.3.2.38 Python representation of architectures](#233238-python-representation-of-architectures)
- [23.3.2.39 Registers In Python](#233239-registers-in-python)
- [23.3.2.40 Connections In Python](#233240-connections-in-python)
- [23.3.2.41 Implementing new TUI windows](#233241-implementing-new-tui-windows)
- [23.3.2.42 Instruction Disassembly In Python](#233242-instruction-disassembly-in-python)
- [23.3.2.43 Missing Debug Info In Python](#233243-missing-debug-info-in-python)
- [23.3.2.44 Missing Objfiles In Python](#233244-missing-objfiles-in-python)
- [23.3.2.45 Core Files In Python](#233245-core-files-in-python)
- [23.3.3 Python Auto-loading](#2333-python-auto-loading)

---

## 23.3.1 Python Commands

GDB provides two commands for accessing the Python interpreter,
and one related setting:

`python-interactive [command]``pi [command]`

Without an argument, the `python-interactive` command can be used
to start an interactive Python prompt. To return to GDB,
type the `EOF` character (e.g., `Ctrl-D` on an empty prompt).

Alternatively, a single-line Python command can be given as an
argument and evaluated. If the command is an expression, the result
will be printed; otherwise, nothing will be printed. For example:

```
(gdb) python-interactive 2 + 3
5
```

`python [command]``py [command]`

The `python` command can be used to evaluate Python code.

If given an argument, the `python` command will evaluate the
argument as a Python command. For example:

```
(gdb) python print(23)
23
```

If you do not provide an argument to `python`, it will act as a
multi-line command, like `define`. In this case, the Python
script is made up of subsequent command lines, given after the
`python` command. This command list is terminated using a line
containing `end`. For example:

```
(gdb) python
>print(23)
>end
23
```

`set python print-stack`

By default, GDB will print only the message component of a
Python exception when an error occurs in a Python script. This can be
controlled using `set python print-stack`: if `full`, then
full Python stack printing is enabled; if `none`, then Python stack
and message printing is disabled; if `message`, the default, only
the message component of the error is printed.

`set python ignore-environment [on|off]`

By default this option is ‘ `off`’, and, when GDB
initializes its internal Python interpreter, the Python interpreter
will check the environment for variables that will effect how it
behaves, for example `PYTHONHOME`, and
`PYTHONPATH` [18](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Python-Commands.html#FOOT18).

If this option is set to ‘ `on`’ before Python is initialized then
Python will ignore all such environment variables. As Python is
initialized early during GDB’s startup process, then this
option must be placed into the early initialization file
(see [Initialization Files](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Initialization-Files.html#Initialization-Files)) to have the desired effect.

This option is equivalent to passing `-E` to the real
`python` executable.

`set python dont-write-bytecode [auto|on|off]`

When this option is ‘ `off`’, then, once GDB has
initialized the Python interpreter, the interpreter will byte-compile
any Python modules that it imports and write the byte code to disk in
`.pyc` files.

If this option is set to ‘ `on`’ before Python is initialized then
Python will no longer write the byte code to disk. As Python is
initialized early during GDB’s startup process, then this
option must be placed into the early initialization file
(see [Initialization Files](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Initialization-Files.html#Initialization-Files)) to have the desired effect.

By default this option is set to ‘ `auto`’. In this mode, provided
the `python ignore-environment` setting is ‘ `off`’, the
environment variable `PYTHONDONTWRITEBYTECODE` is examined to see
if it should write out byte-code or not.
`PYTHONDONTWRITEBYTECODE` is considered to be off/disabled either
when set to the empty string or when the environment variable doesn’t
exist. All other settings, including those which don’t seem to make
sense, indicate that it’s on/enabled.

This option is equivalent to passing `-B` to the real
`python` executable.

It is also possible to execute a Python script from the GDB
interpreter:

`source script-name`

The script name must end with ‘ `.py`’ and GDB must be configured
to recognize the script language based on filename extension using
the `script-extension` setting. See [Extending GDB](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Extending-GDB.html#Extending-GDB).

The following commands are intended to help debug GDB itself:

`set debug py-breakpoint on|off``show debug py-breakpoint`

When ‘ `on`’, GDB prints debug messages related to the
Python breakpoint API. This is ‘ `off`’ by default.

`set debug py-unwind on|off``show debug py-unwind`

When ‘ `on`’, GDB prints debug messages related to the
Python unwinder API. This is ‘ `off`’ by default.


#### Footnotes

### [(18)](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Python-Commands.html\#DOCF18)

See the ENVIRONMENT VARIABLES section of
`man 1 python` for a comprehensive list.

## 23.3.2 Python API

You can get quick online help for GDB's Python API by issuing the command `python help (gdb)`.

Functions and methods which have two or more optional arguments allow them to be specified using keyword syntax. This allows passing some optional arguments while skipping others. Example: `gdb.some_function ('foo', bar = 1, baz = 2)`.

## 23.3.2.1 Basic Python

At startup, GDB overrides Python’s `sys.stdout` and
`sys.stderr` to print using GDB’s output-paging streams.
A Python program which outputs to one of these streams may have its
output interrupted by the user (see [Screen Size](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Screen-Size.html#Screen-Size)). In this
situation, a Python `KeyboardInterrupt` exception is thrown.

Some care must be taken when writing Python code to run in
GDB. Two things worth noting in particular:

- GDB installs handlers for `SIGCHLD` and `SIGINT`.
  Python code must not override these, or even change the options using
  `sigaction`. If your program changes the handling of these
  signals, GDB will most likely stop working correctly. Note
  that it is unfortunately common for GUI toolkits to install a
  `SIGCHLD` handler. When creating a new Python thread, you can
  use `gdb.blocked_signals` or `gdb.Thread` to handle this
  correctly; see [Threading in GDB](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Threading-in-GDB.html#Threading-in-GDB).

- GDB takes care to mark its internal file descriptors as
  close-on-exec. However, this cannot be done in a thread-safe way on
  all platforms. Your Python programs should be aware of this and
  should both create new file descriptors with the close-on-exec flag
  set and arrange to close unneeded file descriptors before starting a
  child process.


GDB introduces a new Python module, named `gdb`. All
methods and classes added by GDB are placed in this module.
GDB automatically `import`s the `gdb` module for
use in all scripts evaluated by the `python` command.

Some types of the `gdb` module come with a textual representation
(accessible through the `repr` or `str` functions). These are
offered for debugging purposes only, expect them to change over time.

Variable: **gdb.PYTHONDIR**

A string containing the python directory (see [Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Python.html#Python)).

Function: **gdb.execute** _(command \[, from\_tty \[, to\_string \[, styling \]\]\])_

Evaluate command, a string, as a GDB CLI command.
If a GDB exception happens while command runs, it is
translated as described in [Exception Handling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Exception-Handling.html#Exception-Handling).

The from\_tty flag specifies whether GDB ought to consider this
command as having originated from the user invoking it interactively.
It must be a boolean value. If omitted, it defaults to `False`.

By default, any output produced by command is sent to
GDB’s standard output (and to the log output if logging is
turned on). If the to\_string parameter is
`True`, then output will be collected by `gdb.execute` and
returned as a string. The default is `False`, in which case the
return value is `None`. If to\_string is `True`, the
GDB virtual terminal will be temporarily set to unlimited width
and height, and its pagination will be disabled; see [Screen Size](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Screen-Size.html#Screen-Size).

When styling is `True`, the output, whether sent to
standard output, or to a string, will have styling applied, if
GDB’s standard output supports styling, and `show style
enabled` is `on`. When styling is `False` then no
styling is applied. The default for styling is `True` when
to\_string is `False`, and `False` when to\_string
is `True`.

Function: **gdb.breakpoints** _()_

Return a sequence holding all of GDB’s breakpoints.
See [Breakpoints In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Breakpoints-In-Python.html#Breakpoints-In-Python), for more information. In GDB
version 7.11 and earlier, this function returned `None` if there
were no breakpoints. This peculiarity was subsequently fixed, and now
`gdb.breakpoints` returns an empty sequence in this case.

Function: **gdb.rbreak** _(regex \[, minsyms \[, throttle, \[, symtabs \]\]\])_

Return a Python list holding a collection of newly set
`gdb.Breakpoint` objects matching function names defined by the
regex pattern. If the minsyms keyword is `True`, all
system functions (those not explicitly defined in the inferior) will
also be included in the match. The throttle keyword takes an
integer that defines the maximum number of pattern matches for
functions matched by the regex pattern. If the number of
matches exceeds the integer value of throttle, a
`RuntimeError` will be raised and no breakpoints will be created.
If throttle is not defined then there is no imposed limit on the
maximum number of matches and breakpoints to be created. The
symtabs keyword takes a Python iterable that yields a collection
of `gdb.Symtab` objects and will restrict the search to those
functions only contained within the `gdb.Symtab` objects.

Function: **gdb.parameter** _(parameter)_

Return the value of a GDBparameter given by its name,
a string; the parameter name string may contain spaces if the parameter has a
multi-part name. For example, ‘ `print object`’ is a valid
parameter name.

If the named parameter does not exist, this function throws a
`gdb.error` (see [Exception Handling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Exception-Handling.html#Exception-Handling)). Otherwise, the
parameter’s value is converted to a Python value of the appropriate
type, and returned.

Function: **gdb.set\_parameter** _(name, value)_

Sets the gdb parameter name to value. As with
`gdb.parameter`, the parameter name string may contain spaces if
the parameter has a multi-part name.

Function: **gdb.with\_parameter** _(name, value)_

Create a Python context manager (for use with the Python
`with` statement) that temporarily sets the gdb parameter
name to value. On exit from the context, the previous
value will be restored.

This uses `gdb.parameter` in its implementation, so it can throw
the same exceptions as that function.

For example, it’s sometimes useful to evaluate some Python code with a
particular gdb language:

```
with gdb.with_parameter('language', 'pascal'):
  ... language-specific operations
```

Function: **gdb.history** _(number)_

Return a value from GDB’s value history (see [Value History](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Value-History.html#Value-History)). The number argument indicates which history element to return.
If number is negative, then GDB will take its absolute value
and count backward from the last element (i.e., the most recent element) to
find the value to return. If number is zero, then GDB will
return the most recent element. If the element specified by number
doesn’t exist in the value history, a `gdb.error` exception will be
raised.

If no exception is raised, the return value is always an instance of
`gdb.Value` (see [Values From Inferior](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Values-From-Inferior.html#Values-From-Inferior)).

Function: **gdb.add\_history** _(value)_

Takes value, an instance of `gdb.Value` (see [Values From Inferior](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Values-From-Inferior.html#Values-From-Inferior)), and appends the value this object represents to
GDB’s value history (see [Value History](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Value-History.html#Value-History)), and return an
integer, its history number. If value is not a
`gdb.Value`, it is is converted using the `gdb.Value`
constructor. If value can’t be converted to a `gdb.Value`
then a `TypeError` is raised.

When a command implemented in Python prints a single `gdb.Value`
as its result, then placing the value into the history will allow the
user convenient access to those values via CLI history facilities.

Function: **gdb.history\_count** _()_

Return an integer indicating the number of values in GDB’s
value history (see [Value History](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Value-History.html#Value-History)).

Function: **gdb.convenience\_variable** _(name)_

Return the value of the convenience variable (see [Convenience Vars](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Convenience-Vars.html#Convenience-Vars)) named name. name must be a string. The name
should not include the ‘ `$`’ that is used to mark a convenience
variable in an expression. If the convenience variable does not
exist, then `None` is returned.

Function: **gdb.set\_convenience\_variable** _(name, value)_

Set the value of the convenience variable (see [Convenience Vars](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Convenience-Vars.html#Convenience-Vars))
named name. name must be a string. The name should not
include the ‘ `$`’ that is used to mark a convenience variable in an
expression. If value is `None`, then the convenience
variable is removed. Otherwise, if value is not a
`gdb.Value` (see [Values From Inferior](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Values-From-Inferior.html#Values-From-Inferior)), it is is converted
using the `gdb.Value` constructor.

Function: **gdb.parse\_and\_eval** _(expression \[, global\_context\])_

Parse expression, which must be a string, as an expression in
the current language, evaluate it, and return the result as a
`gdb.Value`.

global\_context, if provided, is a boolean indicating whether the
parsing should be done in the global context. The default is
‘ `False`’, meaning that the current frame or current static context
should be used.

This function can be useful when implementing a new command
(see [CLI Commands In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/CLI-Commands-In-Python.html#CLI-Commands-In-Python), see [GDB/MI Commands In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Commands-In-Python.html#GDB_002fMI-Commands-In-Python)),
as it provides a way to parse the
command’s argument as an expression. It is also useful simply to
compute values.

Function: **gdb.find\_pc\_line** _(pc)_

Return the `gdb.Symtab_and_line` object corresponding to the
pc value. See [Symbol Tables In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbol-Tables-In-Python.html#Symbol-Tables-In-Python). If an invalid
value of pc is passed as an argument, then the `symtab` and
`line` attributes of the returned `gdb.Symtab_and_line` object
will be `None` and 0 respectively. This is identical to
`gdb.current_progspace().find_pc_line(pc)` and is included for
historical compatibility.

Function: **gdb.write** _(string \[, stream\]\[, style\])_

Print a string to GDB’s paginated output stream. The
optional stream determines the stream to print to. The default
stream is GDB’s standard output stream. Possible stream
values are:

`gdb.STDOUT`

GDB’s standard output stream.

`gdb.STDERR`

GDB’s standard error stream.

`gdb.STDLOG`

GDB’s log stream (see [Logging Output](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Logging-Output.html#Logging-Output)).

The style should be a `gdb.Style` object (see [Styles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Styles-In-Python.html#Styles-In-Python)), or `None` (the default). If style is `None`
then the current style for stream will be applied to text.
If style is a `gdb.Style` object, then this style is
applied to text, after which the default output style is
restored.

Writing to `sys.stdout` or `sys.stderr` will automatically
call this function and will automatically direct the output to the
relevant stream.

Function: **gdb.flush** _(\[stream\])_

Flush the buffer of a GDB paginated stream so that the
contents are displayed immediately. GDB will flush the
contents of a stream automatically when it encounters a newline in the
buffer. The optional stream determines the stream to flush. The
default stream is GDB’s standard output stream. Possible
stream values are:

`gdb.STDOUT`

GDB’s standard output stream.

`gdb.STDERR`

GDB’s standard error stream.

`gdb.STDLOG`

GDB’s log stream (see [Logging Output](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Logging-Output.html#Logging-Output)).

Flushing `sys.stdout` or `sys.stderr` will automatically
call this function for the relevant stream.

Function: **gdb.warning** _(text)_

Print a warning message to GDB’s standard output stream. The
warning message is the warning prefix (see [warning-prefix](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Output-Styling.html#warning_002dprefix)), the
string ‘ `warning: `’, and then text, which must be a
non-empty string.

Due to the warning prefix, text should not begin with a capital
letter (except for proper nouns), and text should end with a
period.

Function: **gdb.target\_charset** _()_

Return the name of the current target character set (see [Character Sets](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Character-Sets.html#Character-Sets)). This differs from `gdb.parameter('target-charset')` in
that ‘ `auto`’ is never returned.

Function: **gdb.target\_wide\_charset** _()_

Return the name of the current target wide character set
(see [Character Sets](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Character-Sets.html#Character-Sets)). This differs from
`gdb.parameter('target-wide-charset')` in that ‘ `auto`’ is
never returned.

Function: **gdb.host\_charset** _()_

Return a string, the name of the current host character set
(see [Character Sets](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Character-Sets.html#Character-Sets)). This differs from
`gdb.parameter('host-charset')` in that ‘ `auto`’ is never
returned.

Function: **gdb.solib\_name** _(address)_

Return the name of the shared library holding the given address
as a string, or `None`. This is identical to
`gdb.current_progspace().solib_name(address)` and is included for
historical compatibility.

Function: **gdb.decode\_line** _(\[expression\])_

Return locations of the line specified by expression, or of the
current line if no argument was given. This function returns a Python
tuple containing two elements. The first element contains a string
holding any unparsed section of expression (or `None` if
the expression has been fully parsed). The second element contains
either `None` or another tuple that contains all the locations
that match the expression represented as `gdb.Symtab_and_line`
objects (see [Symbol Tables In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbol-Tables-In-Python.html#Symbol-Tables-In-Python)). If expression is
provided, it is decoded the way that GDB’s inbuilt
`break` or `edit` commands do (see [Location Specifications](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Location-Specifications.html#Location-Specifications)).

Function: **gdb.prompt\_hook** _(current\_prompt)_

If prompt\_hook is callable, GDB will call the method
assigned to this operation before a prompt is displayed by
GDB.

The parameter `current_prompt` contains the current GDB
prompt. This method must return a Python string, or `None`. If
a string is returned, the GDB prompt will be set to that
string. If `None` is returned, GDB will continue to use
the current prompt.

Some prompts cannot be substituted in GDB. Secondary prompts
such as those used by readline for command input, and annotation
related prompts are prohibited from being changed.

Function: **gdb.architecture\_names** _()_

Return a list containing all of the architecture names that the
current build of GDB supports. Each architecture name is a
string. The names returned in this list are the same names as are
returned from `gdb.Architecture.name`
(see [Architecture.name](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Architectures-In-Python.html#gdbpy_005farchitecture_005fname)).

Function: **gdb.connections**

Return a list of `gdb.TargetConnection` objects, one for each
currently active connection (see [Connections In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Connections-In-Python.html#Connections-In-Python)). The
connection objects are in no particular order in the returned list.

Function: **gdb.format\_address** _(address \[, progspace, architecture\])_

Return a string in the format ‘ `addr
<symbol+offset>`’, where addr is address
formatted in hexadecimal, symbol is the symbol whose address is
the nearest to address and below it in memory, and offset
is the offset from symbol to address in decimal.

If no suitable symbol was found, then the
<symbol+offset\> part is not included in the returned
string, instead the returned string will just contain the
address formatted as hexadecimal. How far GDB looks
back for a suitable symbol can be controlled with `set print
max-symbolic-offset` (see [Print Settings](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Print-Settings.html#Print-Settings)).

Additionally, the returned string can include file name and line
number information when `set print symbol-filename on`
(see [Print Settings](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Print-Settings.html#Print-Settings)), in this case the format of the returned
string is ‘ `addr <symbol+offset> at
filename:line-number`’.

The progspace is the gdb.Progspace in which symbol is
looked up, and architecture is used when formatting addr,
e.g. in order to determine the size of an address in bytes.

If neither progspace or architecture are passed, then by
default GDB will use the program space and architecture of
the currently selected inferior, thus, the following two calls are
equivalent:

```
gdb.format_address(address)
gdb.format_address(address,
                   gdb.selected_inferior().progspace,
                   gdb.selected_inferior().architecture())
```

It is not valid to only pass one of progspace or
architecture, either they must both be provided, or neither must
be provided (and the defaults will be used).

This method uses the same mechanism for formatting address, symbol,
and offset information as core GDB does in commands such as
`disassemble`.

Here are some examples of the possible string formats:

```
0x00001042
0x00001042 <symbol+16>
0x00001042 <symbol+16 at file.c:123>
```

Function: **gdb.current\_language** _()_

Return the name of the current language as a string. Unlike
`gdb.parameter('language')`, this function will never return
‘ `auto`’. If a `gdb.Frame` object is available (see [Frames In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frames-In-Python.html#Frames-In-Python)), the `language` method might be preferable in some
cases, as that is not affected by the user’s language setting.

## 23.3.2.2 Threading in GDB

GDB is not thread-safe. If your Python program uses multiple
threads, you must be careful to only call GDB-specific
functions in the GDB thread. GDB provides some
functions to help with this.

Function: **gdb.blocked\_signals** _()_

As mentioned earlier (see [Basic Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Basic-Python.html#Basic-Python)), certain signals must be
delivered to the GDB main thread. The `blocked_signals`
function returns a context manager that will block these signals on
entry. This can be used when starting a new thread to ensure that the
signals are blocked there, like:

```
with gdb.blocked_signals():
   start_new_thread()
```

class: **gdb.Thread**

This is a subclass of Python’s `threading.Thread` class. It
overrides the `start` method to call `blocked_signals`, making
this an easy-to-use drop-in replacement for creating threads that will
work well in GDB.

Function: **gdb.interrupt** _()_

This causes GDB to react as if the user had typed a control-C
character at the terminal. That is, if the inferior is running, it is
interrupted; if a GDB command is executing, it is stopped;
and if a Python command is running, `KeyboardInterrupt` will be
raised.

Unlike most Python APIs in GDB, `interrupt` is
thread-safe.

Function: **gdb.post\_event** _(event)_

Put event, a callable object taking no arguments, into
GDB’s internal event queue. This callable will be invoked at
some later point, during GDB’s event processing. Events
posted using `post_event` will be run in the order in which they
were posted; however, there is no way to know when they will be
processed relative to other events inside GDB.

Unlike most Python APIs in GDB, `post_event` is
thread-safe. For example:

```
(gdb) python
>import threading
>
>class Writer():
> def __init__(self, message):
>        self.message = message;
> def __call__(self):
>        gdb.write(self.message)
>
>class MyThread1 (threading.Thread):
> def run (self):
>        gdb.post_event(Writer("Hello "))
>
>class MyThread2 (threading.Thread):
> def run (self):
>        gdb.post_event(Writer("World\n"))
>
>MyThread1().start()
>MyThread2().start()
>end
(gdb) Hello World
```

## 23.3.2.3 Exception Handling

When executing the `python` command, Python exceptions
uncaught within the Python code are translated to calls to
GDB error-reporting mechanism. If the command that called
`python` does not handle the error, GDB will
terminate it and print an error message. Exactly what will be printed
depends on `set python print-stack` (see [Python Commands](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Python-Commands.html#Python-Commands)).
Example:

```
(gdb) python print(foo)
Traceback (most recent call last):
  File "<string>", line 1, in <module>
NameError: name 'foo' is not defined
```

GDB errors that happen in GDB commands invoked by
Python code are converted to Python exceptions. The type of the
Python exception depends on the error.

`gdb.error`

This is the base class for most exceptions generated by GDB.
It is derived from `RuntimeError`, for compatibility with earlier
versions of GDB.

If an error occurring in GDB does not fit into some more
specific category, then the generated exception will have this type.

`gdb.MemoryError`

This is a subclass of `gdb.error` which is thrown when an
operation tried to access invalid memory in the inferior.

`KeyboardInterrupt`

User interrupt (via `C-c` or by typing `q` at a pagination
prompt) is translated to a Python `KeyboardInterrupt` exception.

In all cases, your exception handler will see the GDB error
message as its value and the Python call stack backtrace at the Python
statement closest to where the GDB error occurred as the
traceback.

When implementing GDB commands in Python via
`gdb.Command`, or functions via `gdb.Function`, it is useful
to be able to throw an exception that doesn’t cause a traceback to be
printed. For example, the user may have invoked the command
incorrectly. GDB provides a special exception class that can
be used for this purpose.

`gdb.GdbError`

When thrown from a command or function, this exception will cause the
command or function to fail, but the Python stack will not be
displayed. GDB does not throw this exception itself, but
rather recognizes it when thrown from user Python code. Example:

```
(gdb) python
>class HelloWorld (gdb.Command):
>  """Greet the whole world."""
>  def __init__ (self):
>    super (HelloWorld, self).__init__ ("hello-world", gdb.COMMAND_USER)
>  def invoke (self, args, from_tty):
>    argv = gdb.string_to_argv (args)
>    if len (argv) != 0:
>      raise gdb.GdbError ("hello-world takes no arguments")
>    print ("Hello, World!")
>HelloWorld ()
>end
(gdb) hello-world 42
hello-world takes no arguments
```

## 23.3.2.4 Values From Inferior

GDB provides values it obtains from the inferior program in an object of type `gdb.Value`. GDB uses this object for its internal bookkeeping of the inferior’s values, and for fetching values when necessary.

Inferior values that are simple scalars can be used directly in Python expressions that are valid for the value’s data type. Here’s an example for an integer or floating-point value `some_val`:

```python
bar = some_val + 2
```

As result of this, `bar` will also be a `gdb.Value` object whose values are of the same type as those of `some_val`. Valid Python operations can also be performed on `gdb.Value` objects representing a `struct` or `class` object. For such cases, the overloaded operator (if present), is used to perform the operation. For example, if `val1` and `val2` are `gdb.Value` objects representing instances of a `class` which overloads the `+` operator, then one can use the `+` operator in their Python script as follows:

```python
val3 = val1 + val2
```

The result of the operation `val3` is also a `gdb.Value` object corresponding to the value returned by the overloaded `+` operator. In general, overloaded operators are invoked for the following operations: `+` (binary addition), `-` (binary subtraction), `*` (multiplication), `/`, `%`, `<<`, `>>`, `|`, `&`, `^`.

Inferior values that are structures or instances of some class can be accessed using the Python *dictionary syntax*. For example, if `some_val` is a `gdb.Value` instance holding a structure, you can access its `foo` element with:

```python
bar = some_val['foo']
```

Again, `bar` will also be a `gdb.Value` object. Structure elements can also be accessed by using `gdb.Field` objects as subscripts (see [Types In Python](Types-In-Python.html#Types-In-Python), for more information on `gdb.Field` objects). For example, if `foo_field` is a `gdb.Field` object corresponding to element `foo` of the above structure, then `bar` can also be accessed as follows:

```python
bar = some_val[foo_field]
```

If a `gdb.Value` has array or pointer type, an integer index can be used to access elements.

```python
result = some_array[23]
```

A `gdb.Value` that represents a function can be executed via inferior function call. Any arguments provided to the call must match the function’s prototype, and must be provided in the order specified by that prototype.

For example, `some_val` is a `gdb.Value` instance representing a function that takes two integers as arguments. To execute this function, call it like so:

```python
result = some_val (10,20)
```

Any values returned from a function call will be stored as a `gdb.Value`.

The following attributes are provided:

- **Value.address**
  If this object is addressable, this read-only attribute holds a `gdb.Value` object representing the address. Otherwise, this attribute holds `None`.

- **Value.is_optimized_out**
  This read-only boolean attribute is true if the compiler optimized out this value, or any part of this value, and thus it is not available for fetching from the inferior.

- **Value.is_unavailable**
  This read-only boolean attribute is true if this value, or any part of this value, is not available to GDB. Where an optimized out value has been removed from the program by the compiler, an unavailable value does exist in the program, but GDB is unable to fetch it.

  Some reasons why this might occur include, but are not limited to: a core file format that GDB doesn’t fully understand; during live debugging if the debug API has no mechanism to access the required state, e.g. the kernel gives an error when trying to read a particular register set; or reading a value from GDB’s history, when only a partial value was stored, e.g. due to the `max-value-size` setting (see [max-value-size](Value-Sizes.html#set-max_002dvalue_002dsize)).

- **Value.type**
  The type of this `gdb.Value`. The value of this attribute is a `gdb.Type` object (see [Types In Python](Types-In-Python.html#Types-In-Python)).

- **Value.dynamic_type**
  The dynamic type of this `gdb.Value`. This uses the object’s virtual table and the C++ run-time type information (RTTI) to determine the dynamic type of the value. If this value is of class type, it will return the class in which the value is embedded, if any. If this value is of pointer or reference to a class type, it will compute the dynamic type of the referenced object, and return a pointer or reference to that type, respectively. In all other cases, it will return the value’s static type.

  Note that this feature will only work when debugging a C++ program that includes RTTI for the object in question. Otherwise, it will just return the static type of the value as in `ptype foo` (see [ptype](Symbols.html#Symbols)).

- **Value.is_lazy**
  The value of this read-only boolean attribute is `True` if this `gdb.Value` has not yet been fetched from the inferior. GDB does not fetch values until necessary, for efficiency. For example:

  ```python
  myval = gdb.parse_and_eval ('somevar')
  ```

  The value of `somevar` is not fetched at this time. It will be fetched when the value is needed, or when the `fetch_lazy` method is invoked.

- **Value.bytes**
  The value of this attribute is a `bytes` object containing the bytes that make up this `Value`’s complete value in little endian order. If the complete contents of this value are not available then accessing this attribute will raise an exception.

  This attribute can also be assigned to. The new value should be a buffer object (e.g. a `bytes` object), the length of the new buffer must exactly match the length of this `Value`’s type. The bytes values in the new buffer should be in little endian order.

  As with `Value.assign` (see [Value.assign](#Value_002eassign)), if this value cannot be assigned to, then an exception will be thrown.

The following methods are provided:

- **Value.\_\_init\_\_** *(val)*
  Many Python values can be converted directly to a `gdb.Value` via this object initializer. Specifically:
  - **Python boolean**: A Python boolean is converted to the boolean type from the current language.
  - **Python integer**: A Python integer is converted to the C `long` type for the current architecture.
  - **Python long**: A Python long is converted to the C `long long` type for the current architecture.
  - **Python float**: A Python float is converted to the C `double` type for the current architecture.
  - **Python string**: A Python string is converted to a target string in the current target language using the current target encoding. If a character cannot be represented in the current target encoding, then an exception is thrown.
  - **`gdb.Value`**: If `val` is a `gdb.Value`, then a copy of the value is made.
  - **`gdb.LazyString`**: If `val` is a `gdb.LazyString` (see [Lazy Strings In Python](Lazy-Strings-In-Python.html#Lazy-Strings-In-Python)), then the lazy string’s `value` method is called, and its result is used.

- **Value.\_\_init\_\_** *(val, type)*
  This second form of the `gdb.Value` constructor returns a `gdb.Value` of type *type* where the value contents are taken from the Python buffer object specified by *val*. The number of bytes in the Python buffer object must be greater than or equal to the size of *type*.

  If *type* is `None` then this version of `__init__` behaves as though *type* was not passed at all.

- **Value.assign** *(rhs)*
  Assign *rhs* to this value, and return `None`. If this value cannot be assigned to, or if the assignment is invalid for some reason (for example a type-checking failure), an exception will be thrown.

- **Value.cast** *(type)*
  Return a new instance of `gdb.Value` that is the result of casting this instance to the type described by *type*, which must be a `gdb.Type` object. If the cast cannot be performed for some reason, this method throws an exception.

- **Value.dereference** *()*
  For pointer data types, this method returns a new `gdb.Value` object whose contents is the object pointed to by the pointer. For example, if `foo` is a C pointer to an `int`, declared in your C program as

  ```c
  int *foo;
  ```

  then you can use the corresponding `gdb.Value` to access what `foo` points to like this:

  ```python
  bar = foo.dereference ()
  ```

  The result `bar` will be a `gdb.Value` object holding the value pointed to by `foo`.

  A similar function `Value.referenced_value` exists which also returns `gdb.Value` objects corresponding to the values pointed to by pointer values (and additionally, values referenced by reference values). However, the behavior of `Value.dereference` differs from `Value.referenced_value` by the fact that the behavior of `Value.dereference` is identical to applying the C unary operator `*` on a given value. For example, consider a reference to a pointer `ptrref`, declared in your C++ program as

  ```cpp
  typedef int *intptr;
  ...
  int val = 10;
  intptr ptr = &val;
  intptr &ptrref = ptr;
  ```

  Though `ptrref` is a reference value, one can apply the method `Value.dereference` to the `gdb.Value` object corresponding to it and obtain a `gdb.Value` which is identical to that corresponding to `val`. However, if you apply the method `Value.referenced_value`, the result would be a `gdb.Value` object identical to that corresponding to `ptr`.

  ```python
  py_ptrref = gdb.parse_and_eval ("ptrref")
  py_val = py_ptrref.dereference ()
  py_ptr = py_ptrref.referenced_value ()
  ```

  The `gdb.Value` object `py_val` is identical to that corresponding to `val`, and `py_ptr` is identical to that corresponding to `ptr`. In general, `Value.dereference` can be applied whenever the C unary operator `*` can be applied to the corresponding C value. For those cases where applying both `Value.dereference` and `Value.referenced_value` is allowed, the results obtained need not be identical (as we have seen in the above example). The results are however identical when applied on `gdb.Value` objects corresponding to pointers (`gdb.Value` objects with type code `TYPE_CODE_PTR`) in a C/C++ program.

- **Value.referenced_value** *()*
  For pointer or reference data types, this method returns a new `gdb.Value` object corresponding to the value referenced by the pointer/reference value. For pointer data types, `Value.dereference` and `Value.referenced_value` produce identical results. The difference between these methods is that `Value.dereference` cannot get the values referenced by reference values. For example, consider a reference to an `int`, declared in your C++ program as

  ```cpp
  int val = 10;
  int &ref = val;
  ```

  then applying `Value.dereference` to the `gdb.Value` object corresponding to `ref` will result in an error, while applying `Value.referenced_value` will result in a `gdb.Value` object identical to that corresponding to `val`.

  ```python
  py_ref = gdb.parse_and_eval ("ref")
  er_ref = py_ref.dereference ()       # Results in error
  py_val = py_ref.referenced_value ()  # Returns the referenced value
  ```

  The `gdb.Value` object `py_val` is identical to that corresponding to `val`.

- **Value.reference_value** *()*
  Return a `gdb.Value` object which is a reference to the value encapsulated by this instance.

- **Value.const_value** *()*
  Return a `gdb.Value` object which is a `const` version of the value encapsulated by this instance.

- **Value.dynamic_cast** *(type)*
  Like `Value.cast`, but works as if the C++ `dynamic_cast` operator were used. Consult a C++ reference for details.

- **Value.reinterpret_cast** *(type)*
  Like `Value.cast`, but works as if the C++ `reinterpret_cast` operator were used. Consult a C++ reference for details.

- **Value.format_string** *(...)*
  Convert a `gdb.Value` to a string, similarly to what the `print` command does. Invoked with no arguments, this is equivalent to calling the `str` function on the `gdb.Value`. The representation of the same value may change across different versions of GDB, so you shouldn’t, for instance, parse the strings returned by this method.

  All the arguments are keyword only. If an argument is not specified, the current global default setting is used.

  - **`raw`**: `True` if pretty-printers (see [Pretty Printing](Pretty-Printing.html#Pretty-Printing)) should not be used to format the value. `False` if enabled pretty-printers matching the type represented by the `gdb.Value` should be used to format it.
  - **`pretty_arrays`**: `True` if arrays should be pretty printed to be more convenient to read, `False` if they shouldn’t (see `set print array` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`pretty_structs`**: `True` if structs should be pretty printed to be more convenient to read, `False` if they shouldn’t (see `set print pretty` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`array_indexes`**: `True` if array indexes should be included in the string representation of arrays, `False` if they shouldn’t (see `set print array-indexes` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`symbols`**: `True` if the string representation of a pointer should include the corresponding symbol name (if one exists), `False` if it shouldn’t (see `set print symbol` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`unions`**: `True` if unions which are contained in other structures or unions should be expanded, `False` if they shouldn’t (see `set print union` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`address`**: `True` if the string representation of a pointer should include the address, `False` if it shouldn’t (see `set print address` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`nibbles`**: `True` if binary values should be displayed in groups of four bits, known as nibbles. `False` if it shouldn’t (see [set print nibbles](Print-Settings.html#Print-Settings)).
  - **`deref_refs`**: `True` if C++ references should be resolved to the value they refer to, `False` (the default) if they shouldn’t. Note that, unlike for the `print` command, references are not automatically expanded when using the `format_string` method or the `str` function. There is no global `print` setting to change the default behavior.
  - **`actual_objects`**: `True` if the representation of a pointer to an object should identify the *actual* (derived) type of the object rather than the *declared* type, using the virtual function table. `False` if the *declared* type should be used. (See `set print object` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`static_members`**: `True` if static members should be included in the string representation of a C++ object, `False` if they shouldn’t (see `set print static-members` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`max_characters`**: Number of string characters to print, `0` to follow `max_elements`, or `UINT_MAX` to print an unlimited number of characters (see `set print characters` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`max_elements`**: Number of array elements to print, or `0` to print an unlimited number of elements (see `set print elements` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`max_depth`**: The maximum depth to print for nested structs and unions, or `-1` to print an unlimited number of elements (see `set print max-depth` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`repeat_threshold`**: Set the threshold for suppressing display of repeated array elements, or `0` to represent all elements, even if repeated. (See `set print repeats` in [Print Settings](Print-Settings.html#Print-Settings)).
  - **`format`**: A string containing a single character representing the format to use for the returned string. For instance, `'x'` is equivalent to using the GDB command `print` with the `/x` option and formats the value as a hexadecimal number.
  - **`styling`**: `True` if GDB should apply styling to the returned string. When styling is applied, the returned string might contain ANSI terminal escape sequences. Escape sequences will only be included if styling is turned on, see [Output Styling](Output-Styling.html#Output-Styling). Additionally, GDB only styles some value contents, so not every output string will contain escape sequences. When `False`, which is the default, no output styling is applied.
  - **`summary`**: `True` when just a summary should be printed. In this mode, scalar values are printed in their entirety, but aggregates such as structures or unions are omitted. This mode is used by `set print frame-arguments scalars` (see [Print Settings](Print-Settings.html#Print-Settings)).

- **Value.to_array** *()*
  If this value is array-like (see [Type.is_array_like](Types-In-Python.html#Type_002eis_005farray_005flike)), then this method converts it to an array, which is returned. If this value is already an array, it is simply returned. Otherwise, an exception is thrown.

- **Value.string** *([encoding[, errors[, length]]])*
  If this `gdb.Value` represents a string, then this method converts the contents to a Python string. Otherwise, this method will throw an exception.

  Values are interpreted as strings according to the rules of the current language. If the optional length argument is given, the string will be converted to that length, and will include any embedded zeroes that the string may contain. Otherwise, for languages where the string is zero-terminated, the entire string will be converted.

  For example, in C-like languages, a value is a string if it is a pointer to or an array of characters or ints of type `wchar_t`, `char16_t`, or `char32_t`.

  If the optional *encoding* argument is given, it must be a string naming the encoding of the string in the `gdb.Value`, such as `"ascii"`, `"iso-8859-6"` or `"utf-8"`. It accepts the same encodings as the corresponding argument to Python’s `string.decode` method, and the Python codec machinery will be used to convert the string. If *encoding* is not given, or if *encoding* is the empty string, then either the `target-charset` (see [Character Sets](Character-Sets.html#Character-Sets)) will be used, or a language-specific encoding will be used, if the current language is able to supply one.

  The optional *errors* argument is the same as the corresponding argument to Python’s `string.decode` method.

  If the optional *length* argument is given, the string will be fetched and converted to the given length.

- **Value.lazy_string** *([encoding [, length]])*
  This method attempts to convert this `gdb.Value` to a `gdb.LazyString` (see [Lazy Strings In Python](Lazy-Strings-In-Python.html#Lazy-Strings-In-Python)). Values of array or pointer type can be converted; for other types, this method will throw an exception.

  If the optional *encoding* argument is given, it must be a string naming the encoding of the `gdb.LazyString`. Some examples are: ‘ascii’, ‘iso-8859-6’ or ‘utf-8’. If the *encoding* argument is an encoding that GDB does recognize, GDB will raise an error.

  When a lazy string is printed, the GDB encoding machinery is used to convert the string during printing. If the optional *encoding* argument is not provided, or is an empty string, GDB will automatically select the encoding most suitable for the string type. For further information on encoding in GDB please see [Character Sets](Character-Sets.html#Character-Sets).

  If the optional *length* argument is given, the string will be fetched and encoded to the length of characters specified. If the *length* argument is not provided, the string will be fetched and encoded until a null of appropriate width is found.

- **Value.fetch_lazy** *()*
  If the `gdb.Value` object is currently a lazy value (`gdb.Value.is_lazy` is `True`), then the value is fetched from the inferior. Any errors that occur in the process will produce a Python exception.

  If the `gdb.Value` object is not a lazy value, this method has no effect.

  This method does not return a value.

## 23.3.2.5 Types In Python

GDB represents types from the inferior using the class
`gdb.Type`.

The following type-related functions are available in the `gdb`
module:

Function: **gdb.lookup\_type** _(name \[, block\])_

This function looks up a type by its name, which must be a string.

If block is given, then name is looked up in that scope.
Otherwise, it is searched for globally.

Ordinarily, this function will return an instance of `gdb.Type`.
If the named type cannot be found, it will throw an exception.

Integer types can be found without looking them up by name.
See [Architectures In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Architectures-In-Python.html#Architectures-In-Python), for the `integer_type` method.

If the type is a structure or class type, or an enum type, the fields
of that type can be accessed using the Python _dictionary syntax_.
For example, if `some_type` is a `gdb.Type` instance holding
a structure type, you can access its `foo` field with:

```
bar = some_type['foo']
```

`bar` will be a `gdb.Field` object; see below under the
description of the `Type.fields` method for a description of the
`gdb.Field` class.

An instance of `Type` has the following attributes:

Variable: **Type.alignof**

The alignment of this type, in bytes. Type alignment comes from the
debugging information; if it was not specified, then GDB will
use the relevant ABI to try to determine the alignment. In some
cases, even this is not possible, and zero will be returned.

Variable: **Type.code**

The type code for this type. The type code will be one of the
`TYPE_CODE_` constants defined below.

Variable: **Type.dynamic**

A boolean indicating whether this type is dynamic. In some
situations, such as Rust `enum` types or Ada variant records, the
concrete type of a value may vary depending on its contents. That is,
the declared type of a variable, or the type returned by
`gdb.lookup_type` may be dynamic; while the type of the
variable’s value will be a concrete instance of that dynamic type.

For example, consider this code:

```
int n;
int array[n];
```

Here, at least conceptually (whether your compiler actually does this
is a separate issue), examining `gdb.lookup_symbol("array", ...).type`
could yield a `gdb.Type` which reports a size of `None`.
This is the dynamic type.

However, examining `gdb.parse_and_eval("array").type` would yield
a concrete type, whose length would be known.

Variable: **Type.name**

The name of this type. If this type has no name, then `None`
is returned.

Variable: **Type.sizeof**

The size of this type, in target `char` units. Usually, a
target’s `char` type will be an 8-bit byte. However, on some
unusual platforms, this type may have a different size. A dynamic
type may not have a fixed size; in this case, this attribute’s value
will be `None`.

Variable: **Type.tag**

The tag name for this type. The tag name is the name after
`struct`, `union`, or `enum` in C and C `++`; not all
languages have this concept. If this type has no tag name, then
`None` is returned.

Variable: **Type.objfile**

The `gdb.Objfile` that this type was defined in, or `None` if
there is no associated objfile.

Variable: **Type.is\_scalar**

This property is `True` if the type is a scalar type, otherwise,
this property is `False`. Examples of non-scalar types include
structures, unions, and classes.

Variable: **Type.is\_signed**

For scalar types (those for which `Type.is_scalar` is
`True`), this property is `True` if the type is signed,
otherwise this property is `False`.

Attempting to read this property for a non-scalar type (a type for
which `Type.is_scalar` is `False`), will raise a
`ValueError`.

Variable: **Type.is\_array\_like**

A boolean indicating whether this type is array-like.

Some languages have array-like objects that are represented internally
as structures. For example, this is true for a Rust slice type, or
for an Ada unconstrained array. GDB may know about these
types. This determination is done based on the language from which
the type originated.

Variable: **Type.is\_string\_like**

A boolean indicating whether this type is string-like. Like
`Type.is_array_like`, this is determined based on the originating
language of the type.

The following methods are provided:

Function: **Type.fields** _()_

Return the fields of this type. The behavior depends on the type code:

- For structure and union types, this method returns the fields.

- Enum types have one field per enum constant.

- Function and method types have one field per parameter. The base types of
  C `++` classes are also represented as fields.

- Array types have one field representing the array’s range.

- If the type does not fit into one of these categories, a `TypeError`
  is raised.


Each field is a `gdb.Field` object, with some pre-defined attributes:

`bitpos`

This attribute is not available for `enum` or `static`
(as in C `++`) fields. The value is the position, counting
in bits, from the start of the containing type. Note that, in a
dynamic type, the position of a field may not be constant. In this
case, the value will be `None`. Also, a dynamic type may have
fields that do not appear in a corresponding concrete type.

`enumval`

This attribute is only available for `enum` fields, and its value
is the enumeration member’s integer representation.

`name`

The name of the field, or `None` for anonymous fields.

`artificial`

This is `True` if the field is artificial, usually meaning that
it was provided by the compiler and not the user. This attribute is
always provided, and is `False` if the field is not artificial.

`is_base_class`

This is `True` if the field represents a base class of a C `++`
structure. This attribute is always provided, and is `False`
if the field is not a base class of the type that is the argument of
`fields`, or if that type was not a C `++` class.

`bitsize`

If the field is packed, or is a bitfield, then this will have a
non-zero value, which is the size of the field in bits. Otherwise,
this will be zero; in this case the field’s size is given by its type.

`type`

The type of the field. This is usually an instance of `Type`,
but it can be `None` in some situations.

`parent_type`

The type which contains this field. This is an instance of
`gdb.Type`.

Function: **Type.array** _(n1 \[, n2\])_

Return a new `gdb.Type` object which represents an array of this
type. If one argument is given, it is the inclusive upper bound of
the array; in this case the lower bound is zero. If two arguments are
given, the first argument is the lower bound of the array, and the
second argument is the upper bound of the array. An array’s length
must not be negative, but the bounds can be.

Function: **Type.vector** _(n1 \[, n2\])_

Return a new `gdb.Type` object which represents a vector of this
type. If one argument is given, it is the inclusive upper bound of
the vector; in this case the lower bound is zero. If two arguments are
given, the first argument is the lower bound of the vector, and the
second argument is the upper bound of the vector. A vector’s length
must not be negative, but the bounds can be.

The difference between an `array` and a `vector` is that
arrays behave like in C: when used in expressions they decay to a pointer
to the first element whereas vectors are treated as first class values.

Function: **Type.const** _()_

Return a new `gdb.Type` object which represents a
`const`-qualified variant of this type.

Function: **Type.volatile** _()_

Return a new `gdb.Type` object which represents a
`volatile`-qualified variant of this type.

Function: **Type.unqualified** _()_

Return a new `gdb.Type` object which represents an unqualified
variant of this type. That is, the result is neither `const` nor
`volatile`.

Function: **Type.range** _()_

Return a Python `Tuple` object that contains two elements: the
low bound of the argument type and the high bound of that type. If
the type does not have a range, GDB will raise a
`gdb.error` exception (see [Exception Handling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Exception-Handling.html#Exception-Handling)).

Function: **Type.reference** _()_

Return a new `gdb.Type` object which represents a reference to this
type.

Function: **Type.pointer** _()_

Return a new `gdb.Type` object which represents a pointer to this
type.

Function: **Type.strip\_typedefs** _()_

Return a new `gdb.Type` that represents the real type,
after removing all layers of typedefs.

Function: **Type.target** _()_

Return a new `gdb.Type` object which represents the target type
of this type.

For a pointer type, the target type is the type of the pointed-to
object. For an array type (meaning C-like arrays), the target type is
the type of the elements of the array. For a function or method type,
the target type is the type of the return value. For a complex type,
the target type is the type of the elements. For a typedef, the
target type is the aliased type.

If the type does not have a target, this method will throw an
exception.

Function: **Type.template\_argument** _(n \[, block\])_

If this `gdb.Type` is an instantiation of a template, this will
return a new `gdb.Value` or `gdb.Type` which represents the
value of the nth template argument (indexed starting at 0).

If this `gdb.Type` is not a template type, or if the type has fewer
than n template arguments, this will throw an exception.
Ordinarily, only C `++` code will have template types.

If block is given, then name is looked up in that scope.
Otherwise, it is searched for globally.

Function: **Type.optimized\_out** _()_

Return `gdb.Value` instance of this type whose value is optimized
out. This allows a frame decorator to indicate that the value of an
argument or a local variable is not known.

Each type has a code, which indicates what category this type falls
into. The available type categories are represented by constants
defined in the `gdb` module:

`gdb.TYPE_CODE_PTR`

The type is a pointer.

`gdb.TYPE_CODE_ARRAY`

The type is an array.

`gdb.TYPE_CODE_STRUCT`

The type is a structure.

`gdb.TYPE_CODE_UNION`

The type is a union.

`gdb.TYPE_CODE_ENUM`

The type is an enum.

`gdb.TYPE_CODE_FLAGS`

A bit flags type, used for things such as status registers.

`gdb.TYPE_CODE_FUNC`

The type is a function.

`gdb.TYPE_CODE_INT`

The type is an integer type.

`gdb.TYPE_CODE_FLT`

A floating point type.

`gdb.TYPE_CODE_VOID`

The special type `void`.

`gdb.TYPE_CODE_SET`

A Pascal set type.

`gdb.TYPE_CODE_RANGE`

A range type, that is, an integer type with bounds.

`gdb.TYPE_CODE_STRING`

A string type. Note that this is only used for certain languages with
language-defined string types; C strings are not represented this way.

`gdb.TYPE_CODE_BITSTRING`

A string of bits. It is deprecated.

`gdb.TYPE_CODE_ERROR`

An unknown or erroneous type.

`gdb.TYPE_CODE_METHOD`

A method type, as found in C `++`.

`gdb.TYPE_CODE_METHODPTR`

A pointer-to-member-function.

`gdb.TYPE_CODE_MEMBERPTR`

A pointer-to-member.

`gdb.TYPE_CODE_REF`

A reference type.

`gdb.TYPE_CODE_RVALUE_REF`

A C `++` 11 rvalue reference type.

`gdb.TYPE_CODE_CHAR`

A character type.

`gdb.TYPE_CODE_BOOL`

A boolean type.

`gdb.TYPE_CODE_COMPLEX`

A complex float type.

`gdb.TYPE_CODE_TYPEDEF`

A typedef to some other type.

`gdb.TYPE_CODE_NAMESPACE`

A C `++` namespace.

`gdb.TYPE_CODE_DECFLOAT`

A decimal floating point type.

`gdb.TYPE_CODE_INTERNAL_FUNCTION`

A function internal to GDB. This is the type used to represent
convenience functions.

`gdb.TYPE_CODE_XMETHOD`

A method internal to GDB. This is the type used to represent
xmethods (see [Writing an Xmethod](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Writing-an-Xmethod.html#Writing-an-Xmethod)).

`gdb.TYPE_CODE_FIXED_POINT`

A fixed-point number.

`gdb.TYPE_CODE_NAMESPACE`

A Fortran namelist.

Further support for types is provided in the `gdb.types`
Python module (see [gdb.types](https://sourceware.org/gdb/current/onlinedocs/gdb.html/gdb_002etypes.html#gdb_002etypes)).

## 23.3.2.6 Pretty Printing API

A pretty-printer is just an object that holds a value and implements a
specific interface, defined here. An example output is provided
(see [Pretty Printing](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Pretty-Printing.html#Pretty-Printing)).

Because GDB did not document extensibility for
pretty-printers, by default GDB will assume that only the
basic pretty-printer methods may be available. The basic methods are
marked as such, below.

To allow extensibility, GDB provides the
`gdb.ValuePrinter` base class. This class does not provide any
attributes or behavior, but instead serves as a tag that can be
recognized by GDB. For such printers, GDB reserves
all attributes starting with a lower-case letter. That is, in the
future, GDB may add a new method or attribute to the
pretty-printer protocol, and `gdb.ValuePrinter`-based printers
are expected to handle this gracefully. A simple way to do this would
be to use a leading underscore (or two, following the Python
name-mangling scheme) to any attributes local to the implementation.

Function: **pretty\_printer.children** _(self)_

GDB will call this method on a pretty-printer to compute the
children of the pretty-printer’s value.

This method must return an object conforming to the Python iterator
protocol. Each item returned by the iterator must be a tuple holding
two elements. The first element is the “name” of the child; the
second element is the child’s value. The value can be any Python
object which is convertible to a GDB value.

This is a basic method, and is optional. If it does not exist,
GDB will act as though the value has no children.

For efficiency, the `children` method should lazily compute its
results. This will let GDB read as few elements as
necessary, for example when various print settings (see [Print Settings](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Print-Settings.html#Print-Settings)) or `-var-list-children` (see [GDB/MI Variable Objects](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Variable-Objects.html#GDB_002fMI-Variable-Objects)) limit the number of elements to be displayed.

Children may be hidden from display based on the value of ‘ `set
print max-depth`’ (see [Print Settings](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Print-Settings.html#Print-Settings)).

Function: **pretty\_printer.display\_hint** _(self)_

The CLI may call this method and use its result to change the
formatting of a value. The result will also be supplied to an MI
consumer as a ‘ `displayhint`’ attribute of the variable being
printed.

This is a basic method, and is optional. If it does exist, this
method must return a string or the special value `None`.

Some display hints are predefined by GDB:

‘ `array`’

Indicate that the object being printed is “array-like”. The CLI
uses this to respect parameters such as `set print elements` and
`set print array`.

‘ `map`’

Indicate that the object being printed is “map-like”, and that the
children of this value can be assumed to alternate between keys and
values.

‘ `string`’

Indicate that the object being printed is “string-like”. If the
printer’s `to_string` method returns a Python string of some
kind, then GDB will call its internal language-specific
string-printing function to format the string. For the CLI this means
adding quotation marks, possibly escaping some characters, respecting
`set print elements`, and the like.

The special value `None` causes GDB to apply the default
display rules.

Function: **pretty\_printer.to\_string** _(self)_

GDB will call this method to display the string
representation of the value passed to the object’s constructor.

This is a basic method, and is optional.

When printing from the CLI, if the `to_string` method exists,
then GDB will prepend its result to the values returned by
`children`. Exactly how this formatting is done is dependent on
the display hint, and may change as more hints are added. Also,
depending on the print settings (see [Print Settings](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Print-Settings.html#Print-Settings)), the CLI may
print just the result of `to_string` in a stack trace, omitting
the result of `children`.

If this method returns a string, it is printed verbatim.

Otherwise, if this method returns an instance of `gdb.Value`,
then GDB prints this value. This may result in a call to
another pretty-printer.

If instead the method returns a Python value which is convertible to a
`gdb.Value`, then GDB performs the conversion and prints
the resulting value. Again, this may result in a call to another
pretty-printer. Python scalars (integers, floats, and booleans) and
strings are convertible to `gdb.Value`; other types are not.

Finally, if this method returns `None` then no further operations
are performed in this method and nothing is printed.

If the result is not one of these types, an exception is raised.

Function: **pretty\_printer.num\_children** _()_

This is not a basic method, so GDB will only ever call it for
objects derived from `gdb.ValuePrinter`.

If available, this method should return the number of children.
`None` may be returned if the number can’t readily be computed.

Function: **pretty\_printer.child** _(n)_

This is not a basic method, so GDB will only ever call it for
objects derived from `gdb.ValuePrinter`.

If available, this method should return the child item (that is, a
tuple holding the name and value of this child) indicated by n.
Indices start at zero.

GDB provides a function which can be used to look up the
default pretty-printer for a `gdb.Value`:

Function: **gdb.default\_visualizer** _(value)_

This function takes a `gdb.Value` object as an argument. If a
pretty-printer for this value exists, then it is returned. If no such
printer exists, then this returns `None`.

Normally, a pretty-printer can respect the user’s print settings
(including temporarily applied settings, such as ‘ `/x`’) simply by
calling `Value.format_string` (see [Values From Inferior](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Values-From-Inferior.html#Values-From-Inferior)).
However, these settings can also be queried directly:

Function: **gdb.print\_options** _()_

Return a dictionary whose keys are the valid keywords that can be
given to `Value.format_string`, and whose values are the user’s
settings. During a `print` or other operation, the values will
reflect any flags that are temporarily in effect.

```
(gdb) python print(gdb.print_options ()['max_elements'])
200
```

## 23.3.2.7 Selecting Pretty-Printers

GDB provides several ways to register a pretty-printer:
globally, per program space, and per objfile. When choosing how to
register your pretty-printer, a good rule is to register it with the
smallest scope possible: that is prefer a specific objfile first, then
a program space, and only register a printer globally as a last
resort.

Variable: **gdb.pretty\_printers**

The Python list `gdb.pretty_printers` contains an array of
functions or callable objects that have been registered via addition
as a pretty-printer. Printers in this list are called `global`
printers, they’re available when debugging all inferiors.

Each `gdb.Progspace` contains a `pretty_printers` attribute.
Each `gdb.Objfile` also contains a `pretty_printers`
attribute.

Each function on these lists is passed a single `gdb.Value`
argument and should return a pretty-printer object conforming to the
interface definition above (see [Pretty Printing API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Pretty-Printing-API.html#Pretty-Printing-API)). If a function
cannot create a pretty-printer for the value, it should return
`None`.

GDB first checks the `pretty_printers` attribute of each
`gdb.Objfile` in the current program space and iteratively calls
each enabled lookup routine in the list for that `gdb.Objfile`
until it receives a pretty-printer object.
If no pretty-printer is found in the objfile lists, GDB then
searches the pretty-printer list of the current program space,
calling each enabled function until an object is returned.
After these lists have been exhausted, it tries the global
`gdb.pretty_printers` list, again calling each enabled function until an
object is returned.

The order in which the objfiles are searched is not specified. For a
given list, functions are always invoked from the head of the list,
and iterated over sequentially until the end of the list, or a printer
object is returned.

For various reasons a pretty-printer may not work.
For example, the underlying data structure may have changed and
the pretty-printer is out of date.

The consequences of a broken pretty-printer are severe enough that
GDB provides support for enabling and disabling individual
printers. For example, if `print frame-arguments` is on,
a backtrace can become highly illegible if any argument is printed
with a broken printer.

Pretty-printers are enabled and disabled by attaching an `enabled`
attribute to the registered function or callable object. If this attribute
is present and its value is `False`, the printer is disabled, otherwise
the printer is enabled.

## 23.3.2.8 Writing a Pretty-Printer

A pretty-printer consists of two parts: a lookup function to detect
if the type is supported, and the printer itself.

Here is an example showing how a `std::string` printer might be
written. See [Pretty Printing API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Pretty-Printing-API.html#Pretty-Printing-API), for details on the API this class
must provide. Note that this example uses the `gdb.ValuePrinter`
base class, and is careful to use a leading underscore for its local
state.

```
class StdStringPrinter(gdb.ValuePrinter):
    "Print a std::string"

    def __init__(self, val):
        self.__val = val

    def to_string(self):
        return self.__val['_M_dataplus']['_M_p']

    def display_hint(self):
        return 'string'
```

And here is an example showing how a lookup function for the printer
example above might be written.

```
def str_lookup_function(val):
    lookup_tag = val.type.tag
    if lookup_tag is None:
        return None
    regex = re.compile("^std::basic_string<char,.*>$")
    if regex.match(lookup_tag):
        return StdStringPrinter(val)
    return None
```

The example lookup function extracts the value’s type, and attempts to
match it to a type that it can pretty-print. If it is a type the
printer can pretty-print, it will return a printer object. If not, it
returns `None`.

We recommend that you put your core pretty-printers into a Python
package. If your pretty-printers are for use with a library, we
further recommend embedding a version number into the package name.
This practice will enable GDB to load multiple versions of
your pretty-printers at the same time, because they will have
different names.

You should write auto-loaded code (see [Python Auto-loading](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Python-Auto_002dloading.html#Python-Auto_002dloading)) such that it
can be evaluated multiple times without changing its meaning. An
ideal auto-load file will consist solely of `import`s of your
printer modules, followed by a call to a register pretty-printers with
the current objfile.

Taken as a whole, this approach will scale nicely to multiple
inferiors, each potentially using a different library version.
Embedding a version number in the Python package name will ensure that
GDB is able to load both sets of printers simultaneously.
Then, because the search for pretty-printers is done by objfile, and
because your auto-loaded code took care to register your library’s
printers with a specific objfile, GDB will find the correct
printers for the specific version of the library used by each
inferior.

To continue the `std::string` example (see [Pretty Printing API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Pretty-Printing-API.html#Pretty-Printing-API)),
this code might appear in `gdb.libstdcxx.v6`:

```
def register_printers(objfile):
    objfile.pretty_printers.append(str_lookup_function)
```

And then the corresponding contents of the auto-load file would be:

```
import gdb.libstdcxx.v6
gdb.libstdcxx.v6.register_printers(gdb.current_objfile())
```

The previous example illustrates a basic pretty-printer.
There are a few things that can be improved on.
The printer doesn’t have a name, making it hard to identify in a
list of installed printers. The lookup function has a name, but
lookup functions can have arbitrary, even identical, names.

Second, the printer only handles one type, whereas a library typically has
several types. One could install a lookup function for each desired type
in the library, but one could also have a single lookup function recognize
several types. The latter is the conventional way this is handled.
If a pretty-printer can handle multiple data types, then its
_subprinters_ are the printers for the individual data types.

The `gdb.printing` module provides a formal way of solving these
problems (see [gdb.printing](https://sourceware.org/gdb/current/onlinedocs/gdb.html/gdb_002eprinting.html#gdb_002eprinting)).
Here is another example that handles multiple types.

These are the types we are going to pretty-print:

```
struct foo { int a, b; };
struct bar { struct foo x, y; };
```

Here are the printers:

```
class fooPrinter(gdb.ValuePrinter):
    """Print a foo object."""

    def __init__(self, val):
        self.__val = val

    def to_string(self):
        return ("a=<" + str(self.__val["a"]) +
                "> b=<" + str(self.__val["b"]) + ">")

class barPrinter(gdb.ValuePrinter):
    """Print a bar object."""

    def __init__(self, val):
        self.__val = val

    def to_string(self):
        return ("x=<" + str(self.__val["x"]) +
                "> y=<" + str(self.__val["y"]) + ">")
```

This example doesn’t need a lookup function, that is handled by the
`gdb.printing` module. Instead a function is provided to build up
the object that handles the lookup.

```
import gdb.printing

def build_pretty_printer():
    pp = gdb.printing.RegexpCollectionPrettyPrinter(
        "my_library")
    pp.add_printer('foo', '^foo$', fooPrinter)
    pp.add_printer('bar', '^bar$', barPrinter)
    return pp
```

And here is the autoload support:

```
import gdb.printing
import my_library
gdb.printing.register_pretty_printer(
    gdb.current_objfile(),
    my_library.build_pretty_printer())
```

Finally, when this printer is loaded into GDB, here is the
corresponding output of ‘ `info pretty-printer`’:

```
(gdb) info pretty-printer
my_library.so:
  my_library
    foo
    bar
```

## 23.3.2.9 Type Printing API

GDB provides a way for Python code to customize type display.
This is mainly useful for substituting canonical typedef names for
types.

A _type printer_ is just a Python object conforming to a certain
protocol. A simple base class implementing the protocol is provided;
see [gdb.types](https://sourceware.org/gdb/current/onlinedocs/gdb.html/gdb_002etypes.html#gdb_002etypes). A type printer must supply at least:

Instance Variable of type\_printer: **enabled**

A boolean which is True if the printer is enabled, and False
otherwise. This is manipulated by the `enable type-printer`
and `disable type-printer` commands.

Instance Variable of type\_printer: **name**

The name of the type printer. This must be a string. This is used by
the `enable type-printer` and `disable type-printer`
commands.

Method on type\_printer: **instantiate** _(self)_

This is called by GDB at the start of type-printing. It is
only called if the type printer is enabled. This method must return a
new object that supplies a `recognize` method, as described below.

When displaying a type, say via the `ptype` command, GDB
will compute a list of type recognizers. This is done by iterating
first over the per-objfile type printers (see [Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfiles-In-Python)),
followed by the per-progspace type printers (see [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python)), and finally the global type printers.

GDB will call the `instantiate` method of each enabled
type printer. If this method returns `None`, then the result is
ignored; otherwise, it is appended to the list of recognizers.

Then, when GDB is going to display a type name, it iterates
over the list of recognizers. For each one, it calls the recognition
function, stopping if the function returns a non-`None` value.
The recognition function is defined as:

Method on type\_recognizer: **recognize** _(self, type)_

If type is not recognized, return `None`. Otherwise,
return a string which is to be printed as the name of type.
The type argument will be an instance of `gdb.Type`
(see [Types In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Types-In-Python.html#Types-In-Python)).

GDB uses this two-pass approach so that type printers can
efficiently cache information without holding on to it too long. For
example, it can be convenient to look up type information in a type
printer and hold it for a recognizer’s lifetime; if a single pass were
done then type printers would have to make use of the event system in
order to avoid holding information that could become stale as the
inferior changed.

## 23.3.2.10 Filtering Frames

Frame filters are Python objects that manipulate the visibility of a
frame or frames when a backtrace (see [Backtrace](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Backtrace.html#Backtrace)) is printed by
GDB.

Only commands that print a backtrace, or, in the case of GDB/MI
commands (see [GDB/MI](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI.html#GDB_002fMI)), those that return a collection of frames
are affected. The commands that work with frame filters are:

`backtrace` (see [The backtrace command](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Backtrace.html#backtrace_002dcommand)),
`-stack-list-frames`
(see [The -stack-list-frames command](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Stack-Manipulation.html#g_t_002dstack_002dlist_002dframes)),
`-stack-list-variables` (see [The\\
-stack-list-variables command](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Stack-Manipulation.html#g_t_002dstack_002dlist_002dvariables)), `-stack-list-arguments`
see [The -stack-list-arguments command](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Stack-Manipulation.html#g_t_002dstack_002dlist_002darguments)) and
`-stack-list-locals` (see [The\\
-stack-list-locals command](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Stack-Manipulation.html#g_t_002dstack_002dlist_002dlocals)).

A frame filter works by taking an iterator as an argument, applying
actions to the contents of that iterator, and returning another
iterator (or, possibly, the same iterator it was provided in the case
where the filter does not perform any operations). Typically, frame
filters utilize tools such as the Python’s `itertools` module to
work with and create new iterators from the source iterator.
Regardless of how a filter chooses to apply actions, it must not alter
the underlying GDB frame or frames, or attempt to alter the
call-stack within GDB. This preserves data integrity within
GDB. Frame filters are executed on a priority basis and care
should be taken that some frame filters may have been executed before,
and that some frame filters will be executed after.

An important consideration when designing frame filters, and well
worth reflecting upon, is that frame filters should avoid unwinding
the call stack if possible. Some stacks can run very deep, into the
tens of thousands in some cases. To search every frame when a frame
filter executes may be too expensive at that step. The frame filter
cannot know how many frames it has to iterate over, and it may have to
iterate through them all. This ends up duplicating effort as
GDB performs this iteration when it prints the frames. If
the filter can defer unwinding frames until frame decorators are
executed, after the last filter has executed, it should. See [Frame Decorator API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frame-Decorator-API.html#Frame-Decorator-API), for more information on decorators. Also, there are
examples for both frame decorators and filters in later chapters.
See [Writing a Frame Filter](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Writing-a-Frame-Filter.html#Writing-a-Frame-Filter), for more information.

The Python dictionary `gdb.frame_filters` contains key/object
pairings that comprise a frame filter. Frame filters in this
dictionary are called `global` frame filters, and they are
available when debugging all inferiors. These frame filters must
register with the dictionary directly. In addition to the
`global` dictionary, there are other dictionaries that are loaded
with different inferiors via auto-loading (see [Python Auto-loading](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Python-Auto_002dloading.html#Python-Auto_002dloading)). The two other areas where frame filter dictionaries
can be found are: `gdb.Progspace` which contains a
`frame_filters` dictionary attribute, and each `gdb.Objfile`
object which also contains a `frame_filters` dictionary
attribute.

When a command is executed from GDB that is compatible with
frame filters, GDB combines the `global`,
`gdb.Progspace` and all `gdb.Objfile` dictionaries currently
loaded. All of the `gdb.Objfile` dictionaries are combined, as
several frames, and thus several object files, might be in use.
GDB then prunes any frame filter whose `enabled`
attribute is `False`. This pruned list is then sorted according
to the `priority` attribute in each filter.

Once the dictionaries are combined, pruned and sorted, GDB
creates an iterator which wraps each frame in the call stack in a
`FrameDecorator` object, and calls each filter in order. The
output from the previous filter will always be the input to the next
filter, and so on.

Frame filters have a mandatory interface which each frame filter must
implement, defined here:

Function: **FrameFilter.filter** _(iterator)_

GDB will call this method on a frame filter when it has
reached the order in the priority list for that filter.

For example, if there are four frame filters:

```
Name         Priority

Filter1      5
Filter2      10
Filter3      100
Filter4      1
```

The order that the frame filters will be called is:

```
Filter3 -> Filter2 -> Filter1 -> Filter4
```

Note that the output from `Filter3` is passed to the input of
`Filter2`, and so on.

This `filter` method is passed a Python iterator. This iterator
contains a sequence of frame decorators that wrap each
`gdb.Frame`, or a frame decorator that wraps another frame
decorator. The first filter that is executed in the sequence of frame
filters will receive an iterator entirely comprised of default
`FrameDecorator` objects. However, after each frame filter is
executed, the previous frame filter may have wrapped some or all of
the frame decorators with their own frame decorator. As frame
decorators must also conform to a mandatory interface, these
decorators can be assumed to act in a uniform manner (see [Frame Decorator API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frame-Decorator-API.html#Frame-Decorator-API)).

This method must return an object conforming to the Python iterator
protocol. Each item in the iterator must be an object conforming to
the frame decorator interface. If a frame filter does not wish to
perform any operations on this iterator, it should return that
iterator untouched.

This method is not optional. If it does not exist, GDB will
raise and print an error.

Variable: **FrameFilter.name**

The `name` attribute must be Python string which contains the
name of the filter displayed by GDB (see [Frame Filter Management](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frame-Filter-Management.html#Frame-Filter-Management)). This attribute may contain any combination of letters
or numbers. Care should be taken to ensure that it is unique. This
attribute is mandatory.

Variable: **FrameFilter.enabled**

The `enabled` attribute must be Python boolean. This attribute
indicates to GDB whether the frame filter is enabled, and
should be considered when frame filters are executed. If
`enabled` is `True`, then the frame filter will be executed
when any of the backtrace commands detailed earlier in this chapter
are executed. If `enabled` is `False`, then the frame
filter will not be executed. This attribute is mandatory.

Variable: **FrameFilter.priority**

The `priority` attribute must be Python integer. This attribute
controls the order of execution in relation to other frame filters.
There are no imposed limits on the range of `priority` other than
it must be a valid integer. The higher the `priority` attribute,
the sooner the frame filter will be executed in relation to other
frame filters. Although `priority` can be negative, it is
recommended practice to assume zero is the lowest priority that a
frame filter can be assigned. Frame filters that have the same
priority are executed in unsorted order in that priority slot. This
attribute is mandatory. 100 is a good default priority.

## 23.3.2.11 Decorating Frames

Frame decorators are sister objects to frame filters (see [Frame Filter API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frame-Filter-API.html#Frame-Filter-API)). Frame decorators are applied by a frame filter and can
only be used in conjunction with frame filters.

The purpose of a frame decorator is to customize the printed content
of each `gdb.Frame` in commands where frame filters are executed.
This concept is called decorating a frame. Frame decorators decorate
a `gdb.Frame` with Python code contained within each API call.
This separates the actual data contained in a `gdb.Frame` from
the decorated data produced by a frame decorator. This abstraction is
necessary to maintain integrity of the data contained in each
`gdb.Frame`.

Frame decorators have a mandatory interface, defined below.

GDB already contains a frame decorator called
`FrameDecorator`. This contains substantial amounts of
boilerplate code to decorate the content of a `gdb.Frame`. It is
recommended that other frame decorators inherit and extend this
object, and only to override the methods needed.

`FrameDecorator` is defined in the Python module
`gdb.FrameDecorator`, so your code can import it like:

```
from gdb.FrameDecorator import FrameDecorator
```

Function: **FrameDecorator.elided** _(self)_

The `elided` method groups frames together in a hierarchical
system. An example would be an interpreter, where multiple low-level
frames make up a single call in the interpreted language. In this
example, the frame filter would elide the low-level frames and present
a single high-level frame, representing the call in the interpreted
language, to the user.

The `elided` function must return an iterable and this iterable
must contain the frames that are being elided wrapped in a suitable
frame decorator. If no frames are being elided this function may
return an empty iterable, or `None`. Elided frames are indented
from normal frames in a `CLI` backtrace, or in the case of
GDB/MI, are placed in the `children` field of the eliding
frame.

It is the frame filter’s task to also filter out the elided frames from
the source iterator. This will avoid printing the frame twice.

Function: **FrameDecorator.function** _(self)_

This method returns the name of the function in the frame that is to
be printed.

This method must return a Python string describing the function, or
`None`.

If this function returns `None`, GDB will not print any
data for this field.

Function: **FrameDecorator.address** _(self)_

This method returns the address of the frame that is to be printed.

This method must return a Python numeric integer type of sufficient
size to describe the address of the frame, or `None`.

If this function returns a `None`, GDB will not print
any data for this field.

Function: **FrameDecorator.filename** _(self)_

This method returns the filename and path associated with this frame.

This method must return a Python string containing the filename and
the path to the object file backing the frame, or `None`.

If this function returns a `None`, GDB will not print
any data for this field.

Function: **FrameDecorator.line** _(self):_

This method returns the line number associated with the current
position within the function addressed by this frame.

This method must return a Python integer type, or `None`.

If this function returns a `None`, GDB will not print
any data for this field.

Function: **FrameDecorator.frame\_args** _(self)_

This method must return an iterable, or `None`. Returning an
empty iterable, or `None` means frame arguments will not be
printed for this frame. This iterable must contain objects that
implement two methods, described here.

This object must implement a `symbol` method which takes a
single `self` parameter and must return a `gdb.Symbol`
(see [Symbols In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbols-In-Python.html#Symbols-In-Python)), or a Python string. The object must also
implement a `value` method which takes a single `self`
parameter and must return a `gdb.Value` (see [Values From Inferior](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Values-From-Inferior.html#Values-From-Inferior)), a Python value, or `None`. If the `value`
method returns `None`, and the `argument` method returns a
`gdb.Symbol`, GDB will look-up and print the value of
the `gdb.Symbol` automatically.

A brief example:

```
class SymValueWrapper():

    def __init__(self, symbol, value):
        self.sym = symbol
        self.val = value

    def value(self):
        return self.val

    def symbol(self):
        return self.sym

class SomeFrameDecorator()
...
...
    def frame_args(self):
        args = []
        try:
            block = self.inferior_frame.block()
        except:
            return None

        # Iterate over all symbols in a block.  Only add
        # symbols that are arguments.
        for sym in block:
            if not sym.is_argument:
                continue
            args.append(SymValueWrapper(sym,None))

        # Add example synthetic argument.
        args.append(SymValueWrapper(``foo'', 42))

        return args
```

Function: **FrameDecorator.frame\_locals** _(self)_

This method must return an iterable or `None`. Returning an
empty iterable, or `None` means frame local arguments will not be
printed for this frame.

The object interface, the description of the various strategies for
reading frame locals, and the example are largely similar to those
described in the `frame_args` function, (see [The\\
frame filter frame\_args function](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frame-Decorator-API.html#frame_005fargs)). Below is a modified example:

```
class SomeFrameDecorator()
...
...
    def frame_locals(self):
        vars = []
        try:
            block = self.inferior_frame.block()
        except:
            return None

        # Iterate over all symbols in a block.  Add all
        # symbols, except arguments.
        for sym in block:
            if sym.is_argument:
                continue
            vars.append(SymValueWrapper(sym,None))

        # Add an example of a synthetic local variable.
        vars.append(SymValueWrapper(``bar'', 99))

        return vars
```

Function: **FrameDecorator.inferior\_frame** _(self):_

This method must return the underlying `gdb.Frame` that this
frame decorator is decorating. GDB requires the underlying
frame for internal frame information to determine how to print certain
values when printing a frame.

## 23.3.2.12 Writing a Frame Filter

There are three basic elements that a frame filter must implement: it must correctly implement the documented interface (see [Frame Filter API](Frame-Filter-API.html#Frame-Filter-API)), it must register itself with GDB, and finally, it must decide if it is to work on the data provided by GDB. In all cases, whether it works on the iterator or not, each frame filter must return an iterator. A bare-bones frame filter follows the pattern in the following example.

```python
import gdb

class FrameFilter():

    def __init__(self):
        # Frame filter attribute creation.
        #
        # 'name' is the name of the filter that GDB will display.
        #
        # 'priority' is the priority of the filter relative to other
        # filters.
        #
        # 'enabled' is a boolean that indicates whether this filter is
        # enabled and should be executed.

        self.name = "Foo"
        self.priority = 100
        self.enabled = True

        # Register this frame filter with the global frame_filters
        # dictionary.
        gdb.frame_filters[self.name] = self

    def filter(self, frame_iter):
        # Just return the iterator.
        return frame_iter
```

The frame filter in the example above implements the three requirements for all frame filters. It implements the API, self registers, and makes a decision on the iterator (in this case, it just returns the iterator untouched).

The first step is attribute creation and assignment, and as shown in the comments the filter assigns the following attributes: `name`, `priority` and whether the filter should be enabled with the `enabled` attribute.

The second step is registering the frame filter with the dictionary or dictionaries that the frame filter has interest in. As shown in the comments, this filter just registers itself with the global dictionary `gdb.frame_filters`. As noted earlier, `gdb.frame_filters` is a dictionary that is initialized in the `gdb` module when GDB starts. What dictionary a filter registers with is an important consideration. Generally, if a filter is specific to a set of code, it should be registered either in the `objfile` or `progspace` dictionaries as they are specific to the program currently loaded in GDB. The global dictionary is always present in GDB and is never unloaded. Any filters registered with the global dictionary will exist until GDB exits. To avoid filters that may conflict, it is generally better to register frame filters against the dictionaries that more closely align with the usage of the filter currently in question. See [Python Auto-loading](Python-Auto_002dloading.html#Python-Auto_002dloading), for further information on auto-loading Python scripts.

GDB takes a hands-off approach to frame filter registration, therefore it is the frame filter’s responsibility to ensure registration has occurred, and that any exceptions are handled appropriately. In particular, you may wish to handle exceptions relating to Python dictionary key uniqueness. It is mandatory that the dictionary key is the same as frame filter’s `name` attribute. When a user manages frame filters (see [Frame Filter Management](Frame-Filter-Management.html#Frame-Filter-Management)), the names GDB will display are those contained in the `name` attribute.

The final step of this example is the implementation of the `filter` method. As shown in the example comments, we define the `filter` method and note that the method must take an iterator, and also must return an iterator. In this bare-bones example, the frame filter is not very useful as it just returns the iterator untouched. However this is a valid operation for frame filters that have the `enabled` attribute set, but decide not to operate on any frames.

In the next example, the frame filter operates on all frames and utilizes a frame decorator to perform some work on the frames. See [Frame Decorator API](Frame-Decorator-API.html#Frame-Decorator-API), for further information on the frame decorator interface.

This example works on inlined frames. It highlights frames which are inlined by tagging them with an “[inlined]” tag. By applying a frame decorator to all frames with the Python `itertools imap` method, the example defers actions to the frame decorator. Frame decorators are only processed when GDB prints the backtrace.

This introduces a new decision making topic: whether to perform decision making operations at the filtering step, or at the printing step. In this example’s approach, it does not perform any filtering decisions at the filtering step beyond mapping a frame decorator to each frame. This allows the actual decision making to be performed when each frame is printed. This is an important consideration, and well worth reflecting upon when designing a frame filter. An issue that frame filters should avoid is unwinding the stack if possible. Some stacks can run very deep, into the tens of thousands in some cases. To search every frame to determine if it is inlined ahead of time may be too expensive at the filtering step. The frame filter cannot know how many frames it has to iterate over, and it would have to iterate through them all. This ends up duplicating effort as GDB performs this iteration when it prints the frames.

In this example decision making can be deferred to the printing step. As each frame is printed, the frame decorator can examine each frame in turn when GDB iterates. From a performance viewpoint, this is the most appropriate decision to make as it avoids duplicating the effort that the printing step would undertake anyway. Also, if there are many frame filters unwinding the stack during filtering, it can substantially delay the printing of the backtrace which will result in large memory usage, and a poor user experience.

```python
class InlineFilter():

    def __init__(self):
        self.name = "InlinedFrameFilter"
        self.priority = 100
        self.enabled = True
        gdb.frame_filters[self.name] = self

    def filter(self, frame_iter):
        frame_iter = itertools.imap(InlinedFrameDecorator,
                                    frame_iter)
        return frame_iter
```

This frame filter is somewhat similar to the earlier example, except that the `filter` method applies a frame decorator object called `InlinedFrameDecorator` to each element in the iterator. The `imap` Python method is light-weight. It does not proactively iterate over the iterator, but rather creates a new iterator which wraps the existing one.

Below is the frame decorator for this example.

```python
class InlinedFrameDecorator(FrameDecorator):

    def __init__(self, fobj):
        super(InlinedFrameDecorator, self).__init__(fobj)

    def function(self):
        frame = self.inferior_frame()
        name = str(frame.name())

        if frame.type() == gdb.INLINE_FRAME:
            name = name + " [inlined]"

        return name
```

This frame decorator only defines and overrides the `function` method. It lets the supplied `FrameDecorator`, which is shipped with GDB, perform the other work associated with printing this frame.

The combination of these two objects create this output from a backtrace:

```
#0  0x004004e0 in bar () at inline.c:11
#1  0x00400566 in max [inlined] (b=6, a=12) at inline.c:21
#2  0x00400566 in main () at inline.c:31
```

So in the case of this example, a frame decorator is applied to all frames, regardless of whether they may be inlined or not. As GDB iterates over the iterator produced by the frame filters, GDB executes each frame decorator which then makes a decision on what to print in the `function` callback. Using a strategy like this is a way to defer decisions on the frame content to printing time.

#### Eliding Frames

It might be that the above example is not desirable for representing inlined frames, and a hierarchical approach may be preferred. If we want to hierarchically represent frames, the `elided` frame decorator interface might be preferable.

This example approaches the issue with the `elided` method. This example is quite long, but very simplistic. It is out-of-scope for this section to write a complete example that comprehensively covers all approaches of finding and printing inlined frames. However, this example illustrates the approach an author might use.

This example comprises of three sections.

```python
class InlineFrameFilter():

    def __init__(self):
        self.name = "InlinedFrameFilter"
        self.priority = 100
        self.enabled = True
        gdb.frame_filters[self.name] = self

    def filter(self, frame_iter):
        return ElidingInlineIterator(frame_iter)
```

This frame filter is very similar to the other examples. The only difference is this frame filter is wrapping the iterator provided to it (`frame_iter`) with a custom iterator called `ElidingInlineIterator`. This again defers actions to when GDB prints the backtrace, as the iterator is not traversed until printing.

The iterator for this example is as follows. It is in this section of the example where decisions are made on the content of the backtrace.

```python
class ElidingInlineIterator:
    def __init__(self, ii):
        self.input_iterator = ii

    def __iter__(self):
        return self

    def next(self):
        frame = next(self.input_iterator)

        if frame.inferior_frame().type() != gdb.INLINE_FRAME:
            return frame

        try:
            eliding_frame = next(self.input_iterator)
        except StopIteration:
            return frame
        return ElidingFrameDecorator(eliding_frame, [frame])
```

This iterator implements the Python iterator protocol. When the `next` function is called (when GDB prints each frame), the iterator checks if this frame decorator, `frame`, is wrapping an inlined frame. If it is not, it returns the existing frame decorator untouched. If it is wrapping an inlined frame, it assumes that the inlined frame was contained within the next oldest frame, `eliding_frame`, which it fetches. It then creates and returns a frame decorator, `ElidingFrameDecorator`, which contains both the elided frame, and the eliding frame.

```python
class ElidingInlineDecorator(FrameDecorator):

    def __init__(self, frame, elided_frames):
        super(ElidingInlineDecorator, self).__init__(frame)
        self.frame = frame
        self.elided_frames = elided_frames

    def elided(self):
        return iter(self.elided_frames)
```

This frame decorator overrides one function and returns the inlined frame in the `elided` method. As before it lets `FrameDecorator` do the rest of the work involved in printing this frame. This produces the following output.

```
#0  0x004004e0 in bar () at inline.c:11
#2  0x00400529 in main () at inline.c:25
    #1  0x00400529 in max (b=6, a=12) at inline.c:15
```

In that output, `max` which has been inlined into `main` is printed hierarchically. Another approach would be to combine the `function` method, and the `elided` method to both print a marker in the inlined frame, and also show the hierarchical relationship.

## 23.3.2.13 Unwinding Frames in Python

In GDB terminology “unwinding” is the process of finding the previous frame (that is, caller’s) from the current one. An unwinder has three methods. The first one checks if it can handle given frame (“sniff” it). For the frames it can sniff an unwinder provides two additional methods: it can return frame’s ID, and it can fetch registers from the previous frame. A running GDB maintains a list of the unwinders and calls each unwinder’s sniffer in turn until it finds the one that recognizes the current frame. There is an API to register an unwinder.

The unwinders that come with GDB handle standard frames. However, mixed language applications (for example, an application running Java Virtual Machine) sometimes use frame layouts that cannot be handled by the GDB unwinders. You can write Python code that can handle such custom frames.

You implement a frame unwinder in Python as a class with which has two attributes, `name` and `enabled`, with obvious meanings, and a single method `__call__`, which examines a given frame and returns an object (an instance of `gdb.UnwindInfo class)` describing it. If an unwinder does not recognize a frame, it should return `None`. The code in GDB that enables writing unwinders in Python uses this object to return frame’s ID and previous frame registers when GDB core asks for them.

An unwinder should do as little work as possible. Some otherwise innocuous operations can cause problems (even crashes, as this code is not well-hardened yet). For example, making an inferior call from an unwinder is unadvisable, as an inferior call will reset GDB’s stack unwinding process, potentially causing re-entrant unwinding.

#### Unwinder Input

An object passed to an unwinder (a `gdb.PendingFrame` instance) provides a method to read frame’s registers:

**Function: PendingFrame.read_register *(register)***

    This method returns the contents of *register* in the frame as a `gdb.Value` object. For a description of the acceptable values of *register* see [Frame.read_register](Frames-In-Python.html#gdbpy_005fframe_005fread_005fregister). If *register* does not name a register for the current architecture, this method will throw an exception.

    Note that this method will always return a `gdb.Value` for a valid register name. This does not mean that the value will be valid. For example, you may request a register that an earlier unwinder could not unwind—the value will be unavailable. Instead, the `gdb.Value` returned from this method will be lazy; that is, its underlying bits will not be fetched until it is first used. So, attempting to use such a value will cause an exception at the point of use.

    The type of the returned `gdb.Value` depends on the register and the architecture. It is common for registers to have a scalar type, like `long long`; but many other types are possible, such as pointer, pointer-to-function, floating point or vector types.

It also provides a factory method to create a `gdb.UnwindInfo` instance to be returned to GDB:

**Function: PendingFrame.create_unwind_info *(frame_id)***

    Returns a new `gdb.UnwindInfo` instance identified by given *frame_id*. The *frame_id* is used internally by GDB to identify the frames within the current thread’s stack. The attributes of *frame_id* determine what type of frame is created within GDB:

    **`sp, pc`**

        The frame is identified by the given stack address and PC. The stack address must be chosen so that it is constant throughout the lifetime of the frame, so a typical choice is the value of the stack pointer at the start of the function—in the DWARF standard, this would be the “Call Frame Address”.

        This is the most common case by far. The other cases are documented for completeness but are only useful in specialized situations.

    **`sp, pc, special`**

        The frame is identified by the stack address, the PC, and a “special” address. The special address is used on architectures that can have frames that do not change the stack, but which are still distinct, for example the IA-64, which has a second stack for registers. Both *sp* and *special* must be constant throughout the lifetime of the frame.

    **`sp`**

        The frame is identified by the stack address only. Any other stack frame with a matching *sp* will be considered to match this frame. Inside gdb, this is called a “wild frame”. You will never need this.

    Each attribute value should either be an instance of `gdb.Value` or an integer.

    A helper class is provided in the `gdb.unwinder` module that can be used to represent a frame-id (see [gdb.unwinder.FrameId](#gdb_002eunwinder_002eFrameId)).

**Function: PendingFrame.architecture *()***

    Return the `gdb.Architecture` (see [Architectures In Python](Architectures-In-Python.html#Architectures-In-Python)) for this `gdb.PendingFrame`. This represents the architecture of the particular frame being unwound.

**Function: PendingFrame.level *()***

    Return an integer, the stack frame level for this frame. See [Stack Frames](Frames.html#Frames).

**Function: PendingFrame.name *()***

    Returns the function name of this pending frame, or `None` if it can’t be obtained.

**Function: PendingFrame.is_valid *()***

    Returns true if the `gdb.PendingFrame` object is valid, false if not. A pending frame object becomes invalid when the call to the unwinder, for which the pending frame was created, returns.

    All `gdb.PendingFrame` methods, except this one, will raise an exception if the pending frame object is invalid at the time the method is called.

**Function: PendingFrame.pc *()***

    Returns the pending frame’s resume address.

**Function: PendingFrame.block *()***

    Return the pending frame’s code block (see [Blocks In Python](Blocks-In-Python.html#Blocks-In-Python)). If the frame does not have a block – for example, if there is no debugging information for the code in question – then this will raise a `RuntimeError` exception.

**Function: PendingFrame.function *()***

    Return the symbol for the function corresponding to this pending frame. See [Symbols In Python](Symbols-In-Python.html#Symbols-In-Python).

**Function: PendingFrame.find_sal *()***

    Return the pending frame’s symtab and line object (see [Symbol Tables In Python](Symbol-Tables-In-Python.html#Symbol-Tables-In-Python)).

**Function: PendingFrame.language *()***

    Return the language of this frame, as a string, or None.

#### Unwinder Output: UnwindInfo

Use `PendingFrame.create_unwind_info` method described above to create a `gdb.UnwindInfo` instance. Use the following method to specify caller registers that have been saved in this frame:

**Function: gdb.UnwindInfo.add_saved_register *(register, value)***

    *register* identifies the register, for a description of the acceptable values see [Frame.read_register](Frames-In-Python.html#gdbpy_005fframe_005fread_005fregister). *value* is a register value (a `gdb.Value` object).

#### The `gdb.unwinder` Module

GDB comes with a `gdb.unwinder` module which contains the following classes:

**class: gdb.unwinder.Unwinder**

    The `Unwinder` class is a base class from which user created unwinders can derive, though it is not required that unwinders derive from this class, so long as any user created unwinder has the required `name` and `enabled` attributes.

    **Function: gdb.unwinder.Unwinder.__init__ *(name)***

        The *name* is a string used to reference this unwinder within some GDB commands (see [Managing Registered Unwinders](#Managing-Registered-Unwinders)).

    **Variable: gdb.unwinder.name**

        A read-only attribute which is a string, the name of this unwinder.

    **Variable: gdb.unwinder.enabled**

        A modifiable attribute containing a boolean; when `True`, the unwinder is enabled, and will be used by GDB. When `False`, the unwinder has been disabled, and will not be used.

**class: gdb.unwinder.FrameId**

    This is a class suitable for being used as the frame-id when calling `gdb.PendingFrame.create_unwind_info`. It is not required to use this class, any class with the required attribute (see [gdb.PendingFrame.create_unwind_info](#gdb_002ePendingFrame_002ecreate_005funwind_005finfo)) will be accepted, but in most cases this class will be sufficient.

    `gdb.unwinder.FrameId` has the following method:

    **Function: gdb.unwinder.FrameId.__init__ *(sp, pc, special = `None`)***

        The *sp* and *pc* arguments are required and should be either a `gdb.Value` object, or an integer.

        The *special* argument is optional; if specified, it should be a `gdb.Value` object, or an integer.

    `gdb.unwinder.FrameId` has the following read-only attributes:

    **Variable: gdb.unwinder.sp**

        The *sp* value passed to the constructor.

    **Variable: gdb.unwinder.pc**

        The *pc* value passed to the constructor.

    **Variable: gdb.unwinder.special**

        The *special* value passed to the constructor, or `None` if no such value was passed.

#### Registering an Unwinder

Object files and program spaces can have unwinders registered with them. In addition, you can register unwinders globally.

The `gdb.unwinders` module provides the function to register an unwinder:

**Function: gdb.unwinder.register_unwinder *(locus, unwinder, replace=False)***

    *locus* specifies to which unwinder list to prepend the *unwinder*. It can be either an object file (see [Objfiles In Python](Objfiles-In-Python.html#Objfiles-In-Python)), a program space (see [Progspaces In Python](Progspaces-In-Python.html#Progspaces-In-Python)), or `None`, in which case the unwinder is registered globally. The newly added *unwinder* will be called before any other unwinder from the same locus. Two unwinders in the same locus cannot have the same name. An attempt to add an unwinder with an already existing name raises an exception unless *replace* is `True`, in which case the old unwinder is deleted and the new unwinder is registered in its place.

    GDB first calls the unwinders from all the object files in no particular order, then the unwinders from the current program space, then the globally registered unwinders, and finally the unwinders builtin to GDB.

#### Unwinder Skeleton Code

Here is an example of how to structure a user created unwinder:

```python
from gdb.unwinder import Unwinder, FrameId

class MyUnwinder(Unwinder):
    def __init__(self):
        super().__init___("MyUnwinder_Name")

    def __call__(self, pending_frame):
        if not <we recognize frame>:
            return None

        # Create a FrameID.  Usually the frame is identified by a
        # stack pointer and the function address.
        sp = ... compute a stack address ...
        pc = ... compute function address ...
        unwind_info = pending_frame.create_unwind_info(FrameId(sp, pc))

        # Find the values of the registers in the caller's frame and
        # save them in the result:
        unwind_info.add_saved_register(<register-number>, <register-value>)
        ....

        # Return the result:
        return unwind_info

gdb.unwinder.register_unwinder(<locus>, MyUnwinder(), <replace>)
```

#### Managing Registered Unwinders

GDB defines 3 commands to manage registered unwinders. These are:

**`info unwinder [ *locus* [ *name-regexp* ] ]`**

    Lists all registered unwinders. Arguments *locus* and *name-regexp* are both optional and can be used to filter which unwinders are listed.

    The *locus* argument should be either `global`, `progspace`, or the name of an object file. Only unwinders registered for the specified locus will be listed.

    The *name-regexp* is a regular expression used to match against unwinder names. When trying to match against unwinder names that include a string enclose *name-regexp* in quotes.

**`disable unwinder [ *locus* [ *name-regexp* ] ]`**

    The *locus* and *name-regexp* are interpreted as in `info unwinder` above, but instead of listing the matching unwinders, all of the matching unwinders are disabled. The `enabled` field of each matching unwinder is set to `False`.

**`enable unwinder [ *locus* [ *name-regexp* ] ]`**

    The *locus* and *name-regexp* are interpreted as in `info unwinder` above, but instead of listing the matching unwinders, all of the matching unwinders are enabled. The `enabled` field of each matching unwinder is set to `True`.

## 23.3.2.14 Xmethods In Python

_Xmethods_ are additional methods or replacements for existing
methods of a C `++` class. This feature is useful for those cases
where a method defined in C `++` source code could be inlined or
optimized out by the compiler, making it unavailable to GDB.
For such cases, one can define an xmethod to serve as a replacement
for the method defined in the C `++` source code. GDB will
then invoke the xmethod, instead of the C `++` method, to
evaluate expressions. One can also use xmethods when debugging
with core files. Moreover, when debugging live programs, invoking an
xmethod need not involve running the inferior (which can potentially
perturb its state). Hence, even if the C `++` method is available, it
is better to use its replacement xmethod if one is defined.

The xmethods feature in Python is available via the concepts of an
_xmethod matcher_ and an _xmethod worker_. To
implement an xmethod, one has to implement a matcher and a
corresponding worker for it (more than one worker can be
implemented, each catering to a different overloaded instance of the
method). Internally, GDB invokes the `match` method of a
matcher to match the class type and method name. On a match, the
`match` method returns a list of matching _worker_ objects.
Each worker object typically corresponds to an overloaded instance of
the xmethod. They implement a `get_arg_types` method which
returns a sequence of types corresponding to the arguments the xmethod
requires. GDB uses this sequence of types to perform
overload resolution and picks a winning xmethod worker. A winner
is also selected from among the methods GDB finds in the
C `++` source code. Next, the winning xmethod worker and the
winning C `++` method are compared to select an overall winner. In
case of a tie between a xmethod worker and a C `++` method, the
xmethod worker is selected as the winner. That is, if a winning
xmethod worker is found to be equivalent to the winning C `++`
method, then the xmethod worker is treated as a replacement for
the C `++` method. GDB uses the overall winner to invoke the
method. If the winning xmethod worker is the overall winner, then
the corresponding xmethod is invoked via the `__call__` method
of the worker object.

If one wants to implement an xmethod as a replacement for an
existing C `++` method, then they have to implement an equivalent
xmethod which has exactly the same name and takes arguments of
exactly the same type as the C `++` method. If the user wants to
invoke the C `++` method even though a replacement xmethod is
available for that method, then they can disable the xmethod.

See [Xmethod API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Xmethod-API.html#Xmethod-API), for API to implement xmethods in Python.
See [Writing an Xmethod](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Writing-an-Xmethod.html#Writing-an-Xmethod), for implementing xmethods in Python.

## 23.3.2.15 Xmethod API

The GDB Python API provides classes, interfaces and functions
to implement, register and manipulate xmethods.
See [Xmethods In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Xmethods-In-Python.html#Xmethods-In-Python).

An xmethod matcher should be an instance of a class derived from
`XMethodMatcher` defined in the module `gdb.xmethod`, or an
object with similar interface and attributes. An instance of
`XMethodMatcher` has the following attributes:

Variable: **name**

The name of the matcher.

Variable: **enabled**

A boolean value indicating whether the matcher is enabled or disabled.

Variable: **methods**

A list of named methods managed by the matcher. Each object in the list
is an instance of the class `XMethod` defined in the module
`gdb.xmethod`, or any object with the following attributes:

`name`

Name of the xmethod which should be unique for each xmethod
managed by the matcher.

`enabled`

A boolean value indicating whether the xmethod is enabled or
disabled.

The class `XMethod` is a convenience class with same
attributes as above along with the following constructor:

Function: **XMethod.\_\_init\_\_** _(self, name)_

Constructs an enabled xmethod with name name.

The `XMethodMatcher` class has the following methods:

Function: **XMethodMatcher.\_\_init\_\_** _(self, name)_

Constructs an enabled xmethod matcher with name name. The
`methods` attribute is initialized to `None`.

Function: **XMethodMatcher.match** _(self, class\_type, method\_name)_

Derived classes should override this method. It should return a
xmethod worker object (or a sequence of xmethod worker
objects) matching the class\_type and method\_name.
class\_type is a `gdb.Type` object, and method\_name
is a string value. If the matcher manages named methods as listed in
its `methods` attribute, then only those worker objects whose
corresponding entries in the `methods` list are enabled should be
returned.

An xmethod worker should be an instance of a class derived from
`XMethodWorker` defined in the module `gdb.xmethod`,
or support the following interface:

Function: **XMethodWorker.get\_arg\_types** _(self)_

This method returns a sequence of `gdb.Type` objects corresponding
to the arguments that the xmethod takes. It can return an empty
sequence or `None` if the xmethod does not take any arguments.
If the xmethod takes a single argument, then a single
`gdb.Type` object corresponding to it can be returned.

Function: **XMethodWorker.get\_result\_type** _(self, \*args)_

This method returns a `gdb.Type` object representing the type
of the result of invoking this xmethod.
The args argument is the same tuple of arguments that would be
passed to the `__call__` method of this worker.

Function: **XMethodWorker.\_\_call\_\_** _(self, \*args)_

This is the method which does the _work_ of the xmethod. The
args arguments is the tuple of arguments to the xmethod. Each
element in this tuple is a gdb.Value object. The first element is
always the `this` pointer value.

For GDB to lookup xmethods, the xmethod matchers
should be registered using the following function defined in the module
`gdb.xmethod`:

Function: **register\_xmethod\_matcher** _(locus, matcher, replace=False)_

The `matcher` is registered with `locus`, replacing an
existing matcher with the same name as `matcher` if
`replace` is `True`. `locus` can be a
`gdb.Objfile` object (see [Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfiles-In-Python)), or a
`gdb.Progspace` object (see [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python)), or
`None`. If it is `None`, then `matcher` is registered
globally.

## 23.3.2.16 Writing an Xmethod

Implementing xmethods in Python will require implementing xmethod matchers and xmethod workers (see [Xmethods In Python](Xmethods-In-Python.html#Xmethods-In-Python)). Consider the following C++ class:

```cpp
class MyClass
{
public:
  MyClass (int a) : a_(a) { }

  int geta (void) { return a_; }
  int operator+ (int b);

private:
  int a_;
};

int
MyClass::operator+ (int b)
{
  return a_ + b;
}
```

Let us define two xmethods for the class `MyClass`, one replacing the method `geta`, and another adding an overloaded flavor of `operator+` which takes a `MyClass` argument (the C++ code above already has an overloaded `operator+` which takes an `int` argument). The xmethod matcher can be defined as follows:

```python
class MyClass_geta(gdb.xmethod.XMethod):
    def __init__(self):
        gdb.xmethod.XMethod.__init__(self, 'geta')

    def get_worker(self, method_name):
        if method_name == 'geta':
            return MyClassWorker_geta()


class MyClass_sum(gdb.xmethod.XMethod):
    def __init__(self):
        gdb.xmethod.XMethod.__init__(self, 'sum')

    def get_worker(self, method_name):
        if method_name == 'operator+':
            return MyClassWorker_plus()


class MyClassMatcher(gdb.xmethod.XMethodMatcher):
    def __init__(self):
        gdb.xmethod.XMethodMatcher.__init__(self, 'MyClassMatcher')
        # List of methods 'managed' by this matcher
        self.methods = [MyClass_geta(), MyClass_sum()]

    def match(self, class_type, method_name):
        if class_type.tag != 'MyClass':
            return None
        workers = []
        for method in self.methods:
            if method.enabled:
                worker = method.get_worker(method_name)
                if worker:
                    workers.append(worker)

        return workers
```

Notice that the `match` method of `MyClassMatcher` returns a worker object of type `MyClassWorker_geta` for the `geta` method, and a worker object of type `MyClassWorker_plus` for the `operator+` method. This is done indirectly via helper classes derived from `gdb.xmethod.XMethod`. One does not need to use the `methods` attribute in a matcher as it is optional. However, if a matcher manages more than one xmethod, it is a good practice to list the xmethods in the `methods` attribute of the matcher. This will then facilitate enabling and disabling individual xmethods via the `enable/disable` commands. Notice also that a worker object is returned only if the corresponding entry in the `methods` attribute of the matcher is enabled.

The implementation of the worker classes returned by the matcher setup above is as follows:

```python
class MyClassWorker_geta(gdb.xmethod.XMethodWorker):
    def get_arg_types(self):
        return None

    def get_result_type(self, obj):
        return gdb.lookup_type('int')

    def __call__(self, obj):
        return obj['a_']


class MyClassWorker_plus(gdb.xmethod.XMethodWorker):
    def get_arg_types(self):
        return gdb.lookup_type('MyClass')

    def get_result_type(self, obj):
        return gdb.lookup_type('int')

    def __call__(self, obj, other):
        return obj['a_'] + other['a_']
```

For GDB to actually lookup a xmethod, it has to be registered with it. The matcher defined above is registered with GDB globally as follows:

```
gdb.xmethod.register_xmethod_matcher(None, MyClassMatcher())
```

If an object `obj` of type `MyClass` is initialized in C++ code as follows:

```cpp
MyClass obj(5);
```

then, after loading the Python script defining the xmethod matchers and workers into GDB, invoking the method `geta` or using the operator `+` on `obj` will invoke the xmethods defined above:

```gdb
(gdb) p obj.geta()
$1 = 5

(gdb) p obj + obj
$2 = 10
```

Consider another example with a C++ template class:

```cpp
template <class T>
class MyTemplate
{
public:
  MyTemplate () : dsize_(10), data_ (new T [10]) { }
  ~MyTemplate () { delete [] data_; }\n\n  int footprint (void)\n  {\n    return sizeof (T) * dsize_ + sizeof (MyTemplate<T>);\n  }\n\nprivate:\n  int dsize_;\n  T *data_;\n};\n```

Let us implement an xmethod for the above class which serves as a replacement for the `footprint` method. The full code listing of the xmethod workers and xmethod matchers is as follows:

```python
class MyTemplateWorker_footprint(gdb.xmethod.XMethodWorker):
    def __init__(self, class_type):
        self.class_type = class_type

    def get_arg_types(self):
        return None

    def get_result_type(self):
        return gdb.lookup_type('int')

    def __call__(self, obj):
        return (self.class_type.sizeof +
                obj['dsize_'] *
                self.class_type.template_argument(0).sizeof)


class MyTemplateMatcher_footprint(gdb.xmethod.XMethodMatcher):
    def __init__(self):
        gdb.xmethod.XMethodMatcher.__init__(self, 'MyTemplateMatcher')

    def match(self, class_type, method_name):
        if (re.match('MyTemplate<[ \t\n]*[_a-zA-Z][ _a-zA-Z0-9]*>',
                     class_type.tag) and
            method_name == 'footprint'):
            return MyTemplateWorker_footprint(class_type)
```

Notice that, in this example, we have not used the `methods` attribute of the matcher as the matcher manages only one xmethod. The user can enable/disable this xmethod by enabling/disabling the matcher itself.

## 23.3.2.17 Inferiors In Python

Programs which are being run under GDB are called inferiors
(see [Inferiors Connections and Programs](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-Connections-and-Programs.html#Inferiors-Connections-and-Programs)). Python scripts can access
information about and manipulate inferiors controlled by GDB
via objects of the `gdb.Inferior` class.

The following inferior-related functions are available in the `gdb`
module:

Function: **gdb.inferiors** _()_

Return a tuple containing all inferior objects.

Function: **gdb.selected\_inferior** _()_

Return an object representing the current inferior.

A `gdb.Inferior` object has the following attributes:

Variable: **Inferior.num**

ID of inferior, as assigned by GDB. You can use this to make
Python breakpoints inferior-specific, for example
(see [The Breakpoint.inferior\\
attribute](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Breakpoints-In-Python.html#python_005fbreakpoint_005finferior)).

Variable: **Inferior.connection**

The `gdb.TargetConnection` for this inferior (see [Connections In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Connections-In-Python.html#Connections-In-Python)), or `None` if this inferior has no connection.

Variable: **Inferior.connection\_num**

ID of inferior’s connection as assigned by GDB, or None if
the inferior is not connected to a target. See [Inferiors Connections and Programs](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-Connections-and-Programs.html#Inferiors-Connections-and-Programs). This is equivalent to
`gdb.Inferior.connection.num` in the case where
`gdb.Inferior.connection` is not `None`.

Variable: **Inferior.pid**

Process ID of the inferior, as assigned by the underlying operating
system.

Variable: **Inferior.was\_attached**

Boolean signaling whether the inferior was created using ‘attach’, or
started by GDB itself.

Variable: **Inferior.main\_name**

A string holding the name of this inferior’s “main” function, if it
can be determined. If the name of main is not known, this is
`None`.

Variable: **Inferior.progspace**

The inferior’s program space. See [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python).

Variable: **Inferior.arguments**

The inferior’s command line arguments, if known. This corresponds to
the `set args` and `show args` commands. See [Arguments](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Arguments.html#Arguments).

When accessed, the value is a string holding all the arguments. The
contents are quoted as they would be when passed to the shell. If
there are no arguments, the value is `None`.

Either a string or a sequence of strings can be assigned to this
attribute. When a string is assigned, it is assumed to have any
necessary quoting for the shell; when a sequence is assigned, the
quoting is applied by GDB.

Variable: **Inferior.corefile**

If a core file has been loaded into this inferior (see [core-file command](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Files.html#core_002dfile-command)), then this contains a `gdb.Corefile` object that
represents the loaded core file (see [Core Files In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Core-Files-In-Python.html#Core-Files-In-Python)).

If no core file has been loaded into this inferior, then this
attribute contains `None`.

A `gdb.Inferior` object has the following methods:

Function: **Inferior.is\_valid** _()_

Returns `True` if the `gdb.Inferior` object is valid,
`False` if not. A `gdb.Inferior` object will become invalid
if the inferior no longer exists within GDB. All other
`gdb.Inferior` methods will throw an exception if it is invalid
at the time the method is called.

Function: **Inferior.threads** _()_

This method returns a tuple holding all the threads which are valid
when it is called. If there are no valid threads, the method will
return an empty tuple.

Function: **Inferior.architecture** _()_

Return the `gdb.Architecture` (see [Architectures In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Architectures-In-Python.html#Architectures-In-Python))
for this inferior. This represents the architecture of the inferior
as a whole. Some platforms can have multiple architectures in a
single address space, so this may not match the architecture of a
particular frame (see [Frames In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frames-In-Python.html#Frames-In-Python)).

Function: **Inferior.read\_memory** _(address, length)_

Read length addressable memory units from the inferior, starting
at address. Returns a `memoryview` object, which behaves
much like an array or a string. It can be modified and given to the
`Inferior.write_memory` function.

Function: **Inferior.write\_memory** _(address, buffer \[, length\])_

Write the contents of buffer to the inferior, starting at
address. The buffer parameter must be a Python object
which supports the buffer protocol, i.e., a string, an array or the
object returned from `Inferior.read_memory`. If given, length
determines the number of addressable memory units from buffer to be
written.

Function: **Inferior.search\_memory** _(address, length, pattern)_

Search a region of the inferior memory starting at address with
the given length using the search pattern supplied in
pattern. The pattern parameter must be a Python object
which supports the buffer protocol, i.e., a string, an array or the
object returned from `gdb.read_memory`. Returns a Python `Long`
containing the address where the pattern was found, or `None` if
the pattern could not be found.

Function: **Inferior.thread\_from\_handle** _(handle)_

Return the thread object corresponding to handle, a thread
library specific data structure such as `pthread_t` for pthreads
library implementations.

The function `Inferior.thread_from_thread_handle` provides
the same functionality, but use of `Inferior.thread_from_thread_handle`
is deprecated.

The environment that will be passed to the inferior can be changed
from Python by using the following methods. These methods only take
effect when the inferior is started – they will not affect an
inferior that is already executing.

Function: **Inferior.clear\_env** _()_

Clear the current environment variables that will be passed to this
inferior.

Function: **Inferior.set\_env** _(name, value)_

Set the environment variable name to have the indicated value.
Both parameters must be strings.

Function: **Inferior.unset\_env** _(name)_

Unset the environment variable name. name must be a
string.

One may add arbitrary attributes to `gdb.Inferior` objects in the
usual Python way. This is useful if, for example, one needs to do
some extra record keeping associated with the inferior.

When selecting a name for a new attribute, avoid starting the new
attribute name with a lower case letter; future attributes added by
GDB will start with a lower case letter. Additionally, avoid
starting attribute names with two underscore characters, as these
could clash with Python builtin attribute names.

In this contrived example we record the time when an inferior last
stopped:

```
(gdb) python
import datetime

def thread_stopped(event):
    if event.inferior_thread is not None:
        thread = event.inferior_thread
    else:
        thread = gdb.selected_thread()
    inferior = thread.inferior
    inferior._last_stop_time = datetime.datetime.today()

gdb.events.stop.connect(thread_stopped)
```

```
(gdb) file /tmp/hello
Reading symbols from /tmp/hello...
(gdb) start
Temporary breakpoint 1 at 0x401198: file /tmp/hello.c, line 18.
Starting program: /tmp/hello

Temporary breakpoint 1, main () at /tmp/hello.c:18
18	  printf ("Hello World\n");
(gdb) python print(gdb.selected_inferior()._last_stop_time)
2024-01-04 14:48:41.347036
```

## 23.3.2.18 Events In Python

GDB provides a general event facility so that Python code can be
notified of various state changes, particularly changes that occur in
the inferior.

An _event_ is just an object that describes some state change. The
type of the object and its attributes will vary depending on the details
of the change. All the existing events are described below.

In order to be notified of an event, you must register an event handler
with an _event registry_. An event registry is an object in the
`gdb.events` module which dispatches particular events. A registry
provides methods to register and unregister event handlers:

Function: **EventRegistry.connect** _(object)_

Add the given callable object to the registry. This object will be
called when an event corresponding to this registry occurs.

Function: **EventRegistry.disconnect** _(object)_

Remove the given object from the registry. Once removed, the object
will no longer receive notifications of events.

Here is an example:

```
def exit_handler (event):
    print ("event type: exit")
    if hasattr (event, 'exit_code'):
        print ("exit code: %d" % (event.exit_code))
    else:
        print ("exit code not available")

gdb.events.exited.connect (exit_handler)
```

In the above example we connect our handler `exit_handler` to the
registry `events.exited`. Once connected, `exit_handler` gets
called when the inferior exits. The argument _event_ in this example is
of type `gdb.ExitedEvent`. As you can see in the example the
`ExitedEvent` object has an attribute which indicates the exit code of
the inferior.

Some events can be thread specific when GDB is running in
non-stop mode. When represented in Python, these events all extend
`gdb.ThreadEvent`. This event is a base class and is never
emitted directly; instead, events which are emitted by this or other
modules might extend this event. Examples of these events are
`gdb.BreakpointEvent` and `gdb.ContinueEvent`.
`gdb.ThreadEvent` holds the following attributes:

Variable: **ThreadEvent.inferior\_thread**

In non-stop mode this attribute will be set to the specific thread which was
involved in the emitted event. Otherwise, it will be set to `None`.

The following is a listing of the event registries that are available and
details of the events they emit:

`events.cont`

Emits `gdb.ContinueEvent`, which extends `gdb.ThreadEvent`.
This event indicates that the inferior has been continued after a
stop. For inherited attribute refer to `gdb.ThreadEvent` above.

`events.exited`

Emits `events.ExitedEvent`, which indicates that the inferior has
exited. `events.ExitedEvent` has two attributes:

Variable: **ExitedEvent.exit\_code**

An integer representing the exit code, if available, which the inferior
has returned. (The exit code could be unavailable if, for example,
GDB detaches from the inferior.) If the exit code is unavailable,
the attribute does not exist.

Variable: **ExitedEvent.inferior**

A reference to the inferior which triggered the `exited` event.

`events.stop`

Emits `gdb.StopEvent`, which extends `gdb.ThreadEvent`.

Indicates that the inferior has stopped. All events emitted by this
registry extend `gdb.StopEvent`. As a child of
`gdb.ThreadEvent`, `gdb.StopEvent` will indicate the stopped
thread when GDB is running in non-stop mode. Refer to
`gdb.ThreadEvent` above for more details.

`gdb.StopEvent` has the following additional attributes:

Variable: **StopEvent.details**

A dictionary holding any details relevant to the stop. The exact keys
and values depend on the type of stop, but are identical to the
corresponding MI output (see [GDB/MI Async Records](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Async-Records.html#GDB_002fMI-Async-Records)).

A dictionary was used for this (rather than adding attributes directly
to the event object) so that the MI keys could be used unchanged.

When a `StopEvent` results from a `finish` command, it will
also hold the return value from the function, if that is available.
This will be an entry named ‘ `return-value`’ in the `details`
dictionary. The value of this entry will be a `gdb.Value`
object.

Emits `gdb.SignalEvent`, which extends `gdb.StopEvent`.

This event indicates that the inferior or one of its threads has
received a signal. `gdb.SignalEvent` has the following
attributes:

Variable: **SignalEvent.stop\_signal**

A string representing the signal received by the inferior. A list of possible
signal values can be obtained by running the command `info signals` in
the GDB command prompt.

Also emits `gdb.BreakpointEvent`, which extends
`gdb.StopEvent`.

`gdb.BreakpointEvent` event indicates that one or more breakpoints have
been hit, and has the following attributes:

Variable: **BreakpointEvent.breakpoints**

A sequence containing references to all the breakpoints (type
`gdb.Breakpoint`) that were hit.
See [Breakpoints In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Breakpoints-In-Python.html#Breakpoints-In-Python), for details of the `gdb.Breakpoint` object.

Variable: **BreakpointEvent.breakpoint**

A reference to the first breakpoint that was hit. This attribute is
maintained for backward compatibility and is now deprecated in favor
of the `gdb.BreakpointEvent.breakpoints` attribute.

`events.new_objfile`

Emits `gdb.NewObjFileEvent` which indicates that a new object file has
been loaded by GDB. `gdb.NewObjFileEvent` has one attribute:

Variable: **NewObjFileEvent.new\_objfile**

A reference to the object file (`gdb.Objfile`) which has been loaded.
See [Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfiles-In-Python), for details of the `gdb.Objfile` object.

`events.free_objfile`

Emits `gdb.FreeObjFileEvent` which indicates that an object file
is about to be removed from GDB. One reason this can happen
is when the inferior calls `dlclose`.
`gdb.FreeObjFileEvent` has one attribute:

Variable: **FreeObjFileEvent.objfile**

A reference to the object file (`gdb.Objfile`) which will be unloaded.
See [Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfiles-In-Python), for details of the `gdb.Objfile` object.

`events.clear_objfiles`

Emits `gdb.ClearObjFilesEvent` which indicates that the list of object
files for a program space has been reset.
`gdb.ClearObjFilesEvent` has one attribute:

Variable: **ClearObjFilesEvent.progspace**

A reference to the program space (`gdb.Progspace`) whose objfile list has
been cleared. See [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python).

`events.inferior_call`

Emits events just before and after a function in the inferior is
called by GDB. Before an inferior call, this emits an event
of type `gdb.InferiorCallPreEvent`, and after an inferior call,
this emits an event of type `gdb.InferiorCallPostEvent`.

`gdb.InferiorCallPreEvent`

Indicates that a function in the inferior is about to be called.

Variable: **InferiorCallPreEvent.ptid**

The thread in which the call will be run.

Variable: **InferiorCallPreEvent.address**

The location of the function to be called.

`gdb.InferiorCallPostEvent`

Indicates that a function in the inferior has just been called.

Variable: **InferiorCallPostEvent.ptid**

The thread in which the call was run.

Variable: **InferiorCallPostEvent.address**

The location of the function that was called.

`events.memory_changed`

Emits `gdb.MemoryChangedEvent` which indicates that the memory of the
inferior has been modified by the GDB user, for instance via a
command like `set *addr = value`. The event has the following
attributes:

Variable: **MemoryChangedEvent.address**

The start address of the changed region.

Variable: **MemoryChangedEvent.length**

Length in bytes of the changed region.

`events.register_changed`

Emits `gdb.RegisterChangedEvent` which indicates that a register in the
inferior has been modified by the GDB user.

Variable: **RegisterChangedEvent.frame**

A gdb.Frame object representing the frame in which the register was modified.

Variable: **RegisterChangedEvent.regnum**

Denotes which register was modified.

`events.breakpoint_created`

This is emitted when a new breakpoint has been created. The argument
that is passed is the new `gdb.Breakpoint` object.

`events.breakpoint_modified`

This is emitted when a breakpoint has been modified in some way. The
argument that is passed is the new `gdb.Breakpoint` object.

`events.breakpoint_deleted`

This is emitted when a breakpoint has been deleted. The argument that
is passed is the `gdb.Breakpoint` object. When this event is
emitted, the `gdb.Breakpoint` object will already be in its
invalid state; that is, the `is_valid` method will return
`False`.

`events.before_prompt`

This event carries no payload. It is emitted each time GDB
presents a prompt to the user.

`events.new_inferior`

This is emitted when a new inferior is created. Note that the
inferior is not necessarily running; in fact, it may not even have an
associated executable.

The event is of type `gdb.NewInferiorEvent`. This has a single
attribute:

Variable: **NewInferiorEvent.inferior**

The new inferior, a `gdb.Inferior` object.

`events.inferior_deleted`

This is emitted when an inferior has been deleted. Note that this is
not the same as process exit; it is notified when the inferior itself
is removed, say via `remove-inferiors`.

The event is of type `gdb.InferiorDeletedEvent`. This has a single
attribute:

Variable: **InferiorDeletedEvent.inferior**

The inferior that is being removed, a `gdb.Inferior` object.

`events.new_thread`

This is emitted when GDB notices a new thread. The event is of
type `gdb.NewThreadEvent`, which extends `gdb.ThreadEvent`.
This has a single attribute:

Variable: **NewThreadEvent.inferior\_thread**

The new thread.

`events.thread_exited`

This is emitted when GDB notices a thread has exited. The event
is of type `gdb.ThreadExitedEvent` which extends `gdb.ThreadEvent`.
This has a single attribute:

Variable: **ThreadExitedEvent.inferior\_thread**

The exiting thread.

`events.selected_context`

This is emitted when the user directly, or indirectly, causes the
selected inferior context to change. The context consists of the
currently selected inferior, thread, and frame. Examples commands
that trigger this event are `inferior`, `thread`, and
`frame`.

The event is of type `gdb.SelectedContextEvent` and has the
following attributes:

Variable: **SelectedContextEvent.inferior**

The currently selected inferior. This is of type `gdb.Inferior`
(see [Inferiors In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#Inferiors-In-Python)).

Variable: **SelectedContextEvent.thread**

The currently selected thread. If not `None` then this is of
type `gdb.InferiorThread` (see [Threads In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Threads-In-Python.html#Threads-In-Python)). If
switching to an inferior that is not yet started, then the
`thread` attribute will be `None`.

Variable: **SelectedContextEvent.frame**

The currently selected frame. If not `None` then this is of type
`gdb.Frame` (see [Frames In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frames-In-Python.html#Frames-In-Python)). If switching to an
inferior that is not yet started, then the `frame` attribute will
be `None`.

In some cases GDB might emit the `selected_context`
event even when the context has not changed. The state within the
event will always reflect the state of the current inferior. These
unnecessary events could be removed in future releases of
GDB.

`events.gdb_exiting`

This is emitted when GDB exits. This event is not emitted if
GDB exits as a result of an internal error, or after an
unexpected signal. The event is of type `gdb.GdbExitingEvent`,
which has a single attribute:

Variable: **GdbExitingEvent.exit\_code**

An integer, the value of the exit code GDB will return.

`events.connection_removed`

This is emitted when GDB removes a connection
(see [Connections In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Connections-In-Python.html#Connections-In-Python)). The event is of type
`gdb.ConnectionEvent`. This has a single read-only attribute:

Variable: **ConnectionEvent.connection**

The `gdb.TargetConnection` that is being removed.

`events.executable_changed`

Emits `gdb.ExecutableChangedEvent` which indicates that the
`gdb.Progspace.executable_filename` has changed.

This event is emitted when either the value of
`gdb.Progspace.executable_filename` has changed to name a
different file, or the executable file named by
`gdb.Progspace.executable_filename` has changed on disk, and
GDB has therefore reloaded it.

Variable: **ExecutableChangedEvent.progspace**

The `gdb.Progspace` in which the current executable has changed.
The file name of the updated executable will be visible in
`gdb.Progspace.executable_filename` (see [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python)).

Variable: **ExecutableChangedEvent.reload**

This attribute will be `True` if the value of
`gdb.Progspace.executable_filename` didn’t change, but the file
it names changed on disk instead, and GDB reloaded it.

When this attribute is `False`, the value in
`gdb.Progspace.executable_filename` was changed to name a
different file.

Remember that GDB tracks the executable file and the symbol
file separately, these are visible as
`gdb.Progspace.executable_filename` and
`gdb.Progspace.filename` respectively. When using the `file`
command, GDB updates both of these fields, but the executable
file is updated first, so when this event is emitted, the executable
filename will have changed, but the symbol filename might still hold
its previous value.

`events.corefile_changed`

Emits `gdb.CorefileChangedEvent` which indicates that the core
file associated with a `gdb.Inferior` has changed, either a new
core file has been loaded, or the existing core file has been
unloaded (see [Core Files In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Core-Files-In-Python.html#Core-Files-In-Python)).

Variable: **CorefileChangedEvent.inferior**

The `gdb.Inferior` in which the core file has changed
(see [Inferiors In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#Inferiors-In-Python)).

`events.new_progspace`

This is emitted when GDB adds a new program space
(see [Program Spaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python)). The event
is of type `gdb.NewProgspaceEvent`, and has a single read-only
attribute:

Variable: **NewProgspaceEvent.progspace**

The `gdb.Progspace` that was added to GDB.

No `NewProgspaceEvent` is emitted for the very first program
space, which is assigned to the first inferior. This first program
space is created within GDB before any Python scripts are
sourced.

`events.free_progspace`

This is emitted when GDB removes a program space
(see [Program Spaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python)), for example
as a result of the `remove-inferiors` command
(see [`remove-inferiors`](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-Connections-and-Programs.html#remove_005finferiors_005fcli)). The event is
of type `gdb.FreeProgspaceEvent`, and has a single read-only
attribute:

Variable: **FreeProgspaceEvent.progspace**

The `gdb.Progspace` that is about to be removed from
GDB.

`events.tui_enabled`

This is emitted when the TUI is enabled or disabled. The event is of
type `gdb.TuiEnabledEvent`, which has a single read-only
attribute:

Variable: **TuiEnabledEvent.enabled**

If the TUI has just been enabled, this is `True`; otherwise it is
`False`.

## 23.3.2.19 Threads In Python

Python scripts can access information about, and manipulate inferior threads
controlled by GDB, via objects of the `gdb.InferiorThread` class.

The following thread-related functions are available in the `gdb`
module:

Function: **gdb.selected\_thread** _()_

This function returns the thread object for the selected thread. If there
is no selected thread, this will return `None`.

To get the list of threads for an inferior, use the `Inferior.threads()`
method. See [Inferiors In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#Inferiors-In-Python).

A `gdb.InferiorThread` object has the following attributes:

Variable: **InferiorThread.name**

The name of the thread. If the user specified a name using
`thread name`, then this returns that name. Otherwise, if an
OS-supplied name is available, then it is returned. Otherwise, this
returns `None`.

This attribute can be assigned to. The new value must be a string
object, which sets the new name, or `None`, which removes any
user-specified thread name.

Variable: **InferiorThread.num**

The per-inferior number of the thread, as assigned by GDB.

Variable: **InferiorThread.global\_num**

The global ID of the thread, as assigned by GDB. You can use this to
make Python breakpoints thread-specific, for example
(see [The Breakpoint.thread attribute](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Breakpoints-In-Python.html#python_005fbreakpoint_005fthread)).

Variable: **InferiorThread.ptid**

ID of the thread, as assigned by the operating system. This attribute is a
tuple containing three integers. The first is the Process ID (PID); the second
is the Lightweight Process ID (LWPID), and the third is the Thread ID (TID).
Either the LWPID or TID may be 0, which indicates that the operating system
does not use that identifier.

Variable: **InferiorThread.ptid\_string**

This read-only attribute contains a string representing
`InferiorThread.ptid`. This is the string that GDB uses
in the ‘ `Target Id`’ column in the `info threads` output
(see [‘ `info threads`’](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Threads.html#info_005fthreads)).

Variable: **InferiorThread.inferior**

The inferior this thread belongs to. This attribute is represented as
a `gdb.Inferior` object. This attribute is not writable.

Variable: **InferiorThread.details**

A string containing target specific thread state information. The
format of this string varies by target. If there is no additional
state information for this thread, then this attribute contains
`None`.

For example, on a GNU/Linux system, a thread that is in the
process of exiting will return the string ‘ `Exiting`’. For remote
targets the `details` string will be obtained with the
‘ `qThreadExtraInfo`’ remote packet, if the target supports it
(see [‘ `qThreadExtraInfo`’](https://sourceware.org/gdb/current/onlinedocs/gdb.html/General-Query-Packets.html#qThreadExtraInfo)).

GDB displays the `details` string as part of the
‘ `Target Id`’ column, in the `info threads` output
(see [‘ `info threads`’](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Threads.html#info_005fthreads)).

A `gdb.InferiorThread` object has the following methods:

Function: **InferiorThread.is\_valid** _()_

Returns `True` if the `gdb.InferiorThread` object is valid,
`False` if not. A `gdb.InferiorThread` object will become
invalid if the thread exits, or the inferior that the thread belongs
is deleted. All other `gdb.InferiorThread` methods will throw an
exception if it is invalid at the time the method is called.

Function: **InferiorThread.switch** _()_

This changes GDB’s currently selected thread to the one represented
by this object.

Function: **InferiorThread.is\_stopped** _()_

Return a Boolean indicating whether the thread is stopped.

Function: **InferiorThread.is\_running** _()_

Return a Boolean indicating whether the thread is running.

Function: **InferiorThread.is\_exited** _()_

Return a Boolean indicating whether the thread is exited.

Function: **InferiorThread.handle** _()_

Return the thread object’s handle, represented as a Python `bytes`
object. A `gdb.Value` representation of the handle may be
constructed via `gdb.Value(bufobj, type)` where bufobj is
the Python `bytes` representation of the handle and type is
a `gdb.Type` for the handle type.

One may add arbitrary attributes to `gdb.InferiorThread` objects
in the usual Python way. This is useful if, for example, one needs to
do some extra record keeping associated with the thread.

See [choosing attribute names](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#choosing-attribute-names), for guidance on selecting a suitable
name for new attributes.

In this contrived example we record the time when a thread last
stopped:

```
(gdb) python
import datetime

def thread_stopped(event):
    if event.inferior_thread is not None:
        thread = event.inferior_thread
    else:
        thread = gdb.selected_thread()
    thread._last_stop_time = datetime.datetime.today()

gdb.events.stop.connect(thread_stopped)
```

```
(gdb) file /tmp/hello
Reading symbols from /tmp/hello...
(gdb) start
Temporary breakpoint 1 at 0x401198: file /tmp/hello.c, line 18.
Starting program: /tmp/hello

Temporary breakpoint 1, main () at /tmp/hello.c:18
18	  printf ("Hello World\n");
(gdb) python print(gdb.selected_thread()._last_stop_time)
2024-01-04 14:48:41.347036
```

## 23.3.2.20 Recordings In Python

The following recordings-related functions
(see [Process Record and Replay](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Process-Record-and-Replay.html#Process-Record-and-Replay)) are available in the `gdb`
module:

Function: **gdb.start\_recording** _(\[method\], \[format\])_

Start a recording using the given method and format. If
no format is given, the default format for the recording method
is used. If no method is given, the default method will be used.
Returns a `gdb.Record` object on success. Throw an exception on
failure.

The following strings can be passed as method:

- `"full"`
- `"btrace"`: Possible values for format: `"pt"`,
  `"bts"` or leave out for default format.


Function: **gdb.current\_recording** _()_

Access a currently running recording. Return a `gdb.Record`
object on success. Return `None` if no recording is currently
active.

Function: **gdb.stop\_recording** _()_

Stop the current recording. Throw an exception if no recording is
currently active. All record objects become invalid after this call.

A `gdb.Record` object has the following attributes:

Variable: **Record.method**

A string with the current recording method, e.g. `full` or
`btrace`.

Variable: **Record.format**

A string with the current recording format, e.g. `bt`, `pts` or
`None`.

Variable: **Record.begin**

A method specific instruction object representing the first instruction
in this recording.

Variable: **Record.end**

A method specific instruction object representing the current
instruction, that is not actually part of the recording.

Variable: **Record.replay\_position**

The instruction representing the current replay position. If there is
no replay active, this will be `None`.

Variable: **Record.instruction\_history**

A list with all recorded instructions.

Variable: **Record.function\_call\_history**

A list with all recorded function call segments.

A `gdb.Record` object has the following methods:

Function: **Record.goto** _(instruction)_

Move the replay position to the given instruction.

Function: **Record.clear** _()_

Clear the trace data of the current recording. This forces re-decoding of the
trace for successive commands.

The common `gdb.Instruction` class that recording method specific
instruction objects inherit from, has the following attributes:

Variable: **Instruction.pc**

An integer representing this instruction’s address.

Variable: **Instruction.data**

A `memoryview` object holding the raw instruction data.

Variable: **Instruction.decoded**

A human readable string with the disassembled instruction.

Variable: **Instruction.size**

The size of the instruction in bytes.

Additionally `gdb.RecordInstruction` has the following attributes:

Variable: **RecordInstruction.number**

An integer identifying this instruction. `number` corresponds to
the numbers seen in `record instruction-history`
(see [Process Record and Replay](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Process-Record-and-Replay.html#Process-Record-and-Replay)).

Variable: **RecordInstruction.sal**

A `gdb.Symtab_and_line` object representing the associated symtab
and line of this instruction. May be `None` if no debug information is
available.

Variable: **RecordInstruction.is\_speculative**

A boolean indicating whether the instruction was executed speculatively.

If an error occurred during recording or decoding a recording, this error is
represented by a `gdb.RecordGap` object in the instruction list. It has
the following attributes:

Variable: **RecordGap.number**

An integer identifying this gap. `number` corresponds to the numbers seen
in `record instruction-history` (see [Process Record and Replay](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Process-Record-and-Replay.html#Process-Record-and-Replay)).

Variable: **RecordGap.error\_code**

A numerical representation of the reason for the gap. The value is specific to
the current recording method.

Variable: **RecordGap.error\_string**

A human readable string with the reason for the gap.

Some GDB features write auxiliary information into the execution
history. This information is represented by a `gdb.RecordAuxiliary` object
in the instruction list. It has the following attributes:

Variable: **RecordAuxiliary.number**

An integer identifying this auxiliary. number corresponds to the numbers
seen in `record instruction-history` (see [Process Record and Replay](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Process-Record-and-Replay.html#Process-Record-and-Replay)).

Variable: **RecordAuxiliary.data**

A string representation of the auxiliary data.

A `gdb.RecordFunctionSegment` object has the following attributes:

Variable: **RecordFunctionSegment.number**

An integer identifying this function segment. `number` corresponds to
the numbers seen in `record function-call-history`
(see [Process Record and Replay](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Process-Record-and-Replay.html#Process-Record-and-Replay)).

Variable: **RecordFunctionSegment.symbol**

A `gdb.Symbol` object representing the associated symbol. May be
`None` if no debug information is available.

Variable: **RecordFunctionSegment.level**

An integer representing the function call’s stack level. May be
`None` if the function call is a gap.

Variable: **RecordFunctionSegment.instructions**

A list of `gdb.RecordInstruction` or `gdb.RecordGap` objects
associated with this function call.

Variable: **RecordFunctionSegment.up**

A `gdb.RecordFunctionSegment` object representing the caller’s
function segment. If the call has not been recorded, this will be the
function segment to which control returns. If neither the call nor the
return have been recorded, this will be `None`.

Variable: **RecordFunctionSegment.prev**

A `gdb.RecordFunctionSegment` object representing the previous
segment of this function call. May be `None`.

Variable: **RecordFunctionSegment.next**

A `gdb.RecordFunctionSegment` object representing the next segment of
this function call. May be `None`.

The following example demonstrates the usage of these objects and
functions to create a function that will rewind a record to the last
time a function in a different file was executed. This would typically
be used to track the execution of user provided callback functions in a
library which typically are not visible in a back trace.

```
def bringback ():
    rec = gdb.current_recording ()
    if not rec:
        return

    insn = rec.instruction_history
    if len (insn) == 0:
        return

    try:
        position = insn.index (rec.replay_position)
    except:
        position = -1
    try:
        filename = insn[position].sal.symtab.fullname ()
    except:
        filename = None

    for i in reversed (insn[:position]):
	try:
            current = i.sal.symtab.fullname ()
	except:
            current = None

        if filename == current:
            continue

        rec.goto (i)
        return
```

Another possible application is to write a function that counts the
number of code executions in a given line range. This line range can
contain parts of functions or span across several functions and is not
limited to be contiguous.

```
def countrange (filename, linerange):
    count = 0

    def filter_only (file_name):
        for call in gdb.current_recording ().function_call_history:
            try:
                if file_name in call.symbol.symtab.fullname ():
                    yield call
            except:
                pass

    for c in filter_only (filename):
        for i in c.instructions:
            try:
                if i.sal.line in linerange:
                    count += 1
                    break;
            except:
                    pass

    return count
```

## 23.3.2.21 CLI Commands In Python

You can implement new GDB CLI commands in Python. A CLI
command is implemented using an instance of the `gdb.Command`
class, most commonly using a subclass.

Function: **Command.\_\_init\_\_** _(name, command\_class \[, completer\_class \[, prefix\]\])_

The object initializer for `Command` registers the new command
with GDB. This initializer is normally invoked from the
subclass’ own `__init__` method.

name is the name of the command. If name consists of
multiple words, then the initial words are looked for as prefix
commands. In this case, if one of the prefix commands does not exist,
an exception is raised.

There is no support for multi-line commands.

command\_class should be one of the ‘ `COMMAND_`’ constants
defined below. This argument tells GDB how to categorize the
new command in the help system.

completer\_class is an optional argument. If given, it should be
one of the ‘ `COMPLETE_`’ constants defined below. This argument
tells GDB how to perform completion for this command. If not
given, GDB will attempt to complete using the object’s
`complete` method (see below); if no such method is found, an
error will occur when completion is attempted.

prefix is an optional argument. If `True`, then the new
command is a prefix command; sub-commands of this command may be
registered.

The help text for the new command is taken from the Python
documentation string for the command’s class, if there is one. If no
documentation string is provided, the default value ‘ `This command
is not documented.`’ is used.

Function: **Command.dont\_repeat** _()_

By default, a GDB command is repeated when the user enters a
blank line at the command prompt. A command can suppress this
behavior by invoking the `dont_repeat` method at some point in
its `invoke` method (normally this is done early in case of
exception). This is similar to the user command `dont-repeat`,
see [dont-repeat](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Define.html#Define).

Function: **Command.invoke** _(argument, from\_tty)_

This method is called by GDB when this command is invoked.

argument is a string. It is the argument to the command, after
leading and trailing whitespace has been stripped.

from\_tty is a boolean argument. When true, this means that the
command was entered by the user at the terminal; when false it means
that the command came from elsewhere.

If this method throws an exception, it is turned into a GDB`error` call. Otherwise, the return value is ignored.

For non-prefix commands (see [Command.\_\_init\_\_](https://sourceware.org/gdb/current/onlinedocs/gdb.html/CLI-Commands-In-Python.html#Command_002e_005f_005finit_005f_005f)), the `invoke`
method is required. For prefix commands the `invoke` method is
optional. Only prefix commands that need to handle unknown
sub-commands should implement the `invoke` method.

For prefix commands that don’t implement `invoke`, if the prefix
command is used without a sub-command name then GDB will
display the help text for every sub-command, unless the prefix command
is a `show` sub-command, in which case GDB will list the
values of all sub-commands.

To break argument up into an argv-like string use
`gdb.string_to_argv`. This function behaves identically to
GDB’s internal argument lexer `buildargv`.
It is recommended to use this for consistency.
Arguments are separated by spaces and may be quoted.
Example:

```
print(gdb.string_to_argv ("1 2\ \\\"3 '4 \"5' \"6 '7\""))
['1', '2 "3', '4 "5', "6 '7"]
```

Function: **Command.complete** _(text, word)_

This method is called by GDB when the user attempts
completion on this command. All forms of completion are handled by
this method, that is, the `TAB` and `M-?` key bindings
(see [Completion](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Completion.html#Completion)), and the `complete` command (see [complete](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Help.html#Help)).

The arguments text and word are both strings; text
holds the complete command line up to the cursor’s location, while
word holds the last word of the command line; this is computed
using a word-breaking heuristic.

The `complete` method can return several values:

- If the return value is a sequence, the contents of the sequence are
  used as the completions. It is up to `complete` to ensure that the
  contents actually do complete the word. A zero-length sequence is
  allowed, it means that there were no completions available. Only
  string elements of the sequence are used; other elements in the
  sequence are ignored.

- If the return value is one of the ‘ `COMPLETE_`’ constants defined
  below, then the corresponding GDB-internal completion
  function is invoked, and its result is used.

- All other results are treated as though there were no available
  completions.


When a new command is registered, it must be declared as a member of
some general class of commands. This is used to classify top-level
commands in the on-line help system; note that prefix commands are not
listed under their own category but rather that of their top-level
command. The available classifications are represented by constants
defined in the `gdb` module:

`gdb.COMMAND_NONE`

The command does not belong to any particular class. A command in
this category will not be displayed in any of the help categories.

`gdb.COMMAND_RUNNING`

The command is related to running the inferior. For example,
`start`, `step`, and `continue` are in this category.
Type `help running` at the GDB prompt to see a list of
commands in this category.

`gdb.COMMAND_DATA`

The command is related to data or variables. For example,
`call`, `find`, and `print` are in this category. Type
`help data` at the GDB prompt to see a list of commands
in this category.

`gdb.COMMAND_STACK`

The command has to do with manipulation of the stack. For example,
`backtrace`, `frame`, and `return` are in this
category. Type `help stack` at the GDB prompt to see a
list of commands in this category.

`gdb.COMMAND_FILES`

This class is used for file-related commands. For example,
`file`, `list` and `section` are in this category.
Type `help files` at the GDB prompt to see a list of
commands in this category.

`gdb.COMMAND_SUPPORT`

This should be used for “support facilities”, generally meaning
things that are useful to the user when interacting with GDB,
but not related to the state of the inferior. For example,
`help`, `make`, and `shell` are in this category. Type
`help support` at the GDB prompt to see a list of
commands in this category.

`gdb.COMMAND_STATUS`

The command is an ‘ `info`’-related command, that is, related to the
state of GDB itself. For example, `info`, `macro`,
and `show` are in this category. Type `help status` at the
GDB prompt to see a list of commands in this category.

`gdb.COMMAND_BREAKPOINTS`

The command has to do with breakpoints. For example, `break`,
`clear`, and `delete` are in this category. Type `help
breakpoints` at the GDB prompt to see a list of commands in
this category.

`gdb.COMMAND_TRACEPOINTS`

The command has to do with tracepoints. For example, `trace`,
`actions`, and `tfind` are in this category. Type
`help tracepoints` at the GDB prompt to see a list of
commands in this category.

`gdb.COMMAND_TUI`

The command has to do with the text user interface (see [TUI](https://sourceware.org/gdb/current/onlinedocs/gdb.html/TUI.html#TUI)).
Type `help tui` at the GDB prompt to see a list of
commands in this category.

`gdb.COMMAND_USER`

The command is a general purpose command for the user, and typically
does not fit in one of the other categories.
Type `help user-defined` at the GDB prompt to see
a list of commands in this category, as well as the list of gdb macros
(see [Sequences](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Sequences.html#Sequences)).

`gdb.COMMAND_OBSCURE`

The command is only used in unusual circumstances, or is not of
general interest to users. For example, `checkpoint`,
`fork`, and `stop` are in this category. Type `help
obscure` at the GDB prompt to see a list of commands in this
category.

`gdb.COMMAND_MAINTENANCE`

The command is only useful to GDB maintainers. The
`maintenance` and `flushregs` commands are in this category.
Type `help internals` at the GDB prompt to see a list of
commands in this category.

A new command can use a predefined completion function, either by
specifying it via an argument at initialization, or by returning it
from the `complete` method. These predefined completion
constants are all defined in the `gdb` module:

`gdb.COMPLETE_NONE`

This constant means that no completion should be done.

`gdb.COMPLETE_FILENAME`

This constant means that filename completion should be performed.

`gdb.COMPLETE_LOCATION`

This constant means that location completion should be done.
See [Location Specifications](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Location-Specifications.html#Location-Specifications).

`gdb.COMPLETE_COMMAND`

This constant means that completion should examine GDB
command names.

`gdb.COMPLETE_SYMBOL`

This constant means that completion should be done using symbol names
as the source.

`gdb.COMPLETE_EXPRESSION`

This constant means that completion should be done on expressions.
Often this means completing on symbol names, but some language
parsers also have support for completing on field names.

The following code snippet shows how a trivial CLI command can be
implemented in Python:

```
class HelloWorld (gdb.Command):
  """Greet the whole world."""

  def __init__ (self):
    super (HelloWorld, self).__init__ ("hello-world", gdb.COMMAND_USER)

  def invoke (self, arg, from_tty):
    print ("Hello, World!")

HelloWorld ()
```

The last line instantiates the class, and is necessary to trigger the
registration of the command with GDB. Depending on how the
Python code is read into GDB, you may need to import the
`gdb` module explicitly.

## 23.3.2.22 GDB/MI Commands In Python

It is possible to add GDB/MI (see [GDB/MI](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI.html#GDB_002fMI)) commands
implemented in Python. A GDB/MI command is implemented using an
instance of the `gdb.MICommand` class, most commonly using a
subclass.

Function: **MICommand.\_\_init\_\_** _(name)_

The object initializer for `MICommand` registers the new command
with GDB. This initializer is normally invoked from the
subclass’ own `__init__` method.

name is the name of the command. It must be a valid name of a
GDB/MI command, and in particular must start with a hyphen
(`-`). Reusing the name of a built-in GDB/MI is not
allowed, and a `RuntimeError` will be raised. Using the name
of an GDB/MI command previously defined in Python is allowed, the
previous command will be replaced with the new command.

Function: **MICommand.invoke** _(arguments)_

This method is called by GDB when the new MI command is
invoked.

arguments is a list of strings. Note, that `--thread`
and `--frame` arguments are handled by GDB itself therefore
they do not show up in `arguments`.

If this method raises an exception, then it is turned into a
GDB/MI`^error` response. Only `gdb.GdbError`
exceptions (or its sub-classes) should be used for reporting errors to
users, any other exception type is treated as a failure of the
`invoke` method, and the exception will be printed to the error
stream according to the `set python print-stack` setting
(see [`set python print-stack`](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Python-Commands.html#set_005fpython_005fprint_005fstack)).

If this method returns `None`, then the GDB/MI command will
return a `^done` response with no additional values.

Otherwise, the return value must be a dictionary, which is converted
to a GDB/MIresult-record (see [GDB/MI Output Syntax](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Output-Syntax.html#GDB_002fMI-Output-Syntax)).
The keys of this dictionary must be strings, and are used as
variable names in the result-record, these strings must
comply with the naming rules detailed below. The values of this
dictionary are recursively handled as follows:

- If the value is Python sequence or iterator, it is converted to
  GDB/MIlist with elements converted recursively.

- If the value is Python dictionary, it is converted to
  GDB/MItuple. Keys in that dictionary must be strings,
  which comply with the variable naming rules detailed below.
  Values are converted recursively.

- Otherwise, value is first converted to a Python string using
  `str ()` and then converted to GDB/MIconst.


The strings used for variable names in the GDB/MI output
must follow the following rules; the string must be at least one
character long, the first character must be in the set
`[a-zA-Z]`, while every subsequent character must be in the set
`[-_a-zA-Z0-9]`.

An instance of `MICommand` has the following attributes:

Variable: **MICommand.name**

A string, the name of this GDB/MI command, as was passed to the
`__init__` method. This attribute is read-only.

Variable: **MICommand.installed**

A boolean value indicating if this command is installed ready for a
user to call from the command line. Commands are automatically
installed when they are instantiated, after which this attribute will
be `True`.

If later, a new command is created with the same name, then the
original command will become uninstalled, and this attribute will be
`False`.

This attribute is read-write, setting this attribute to `False`
will uninstall the command, removing it from the set of available
commands. Setting this attribute to `True` will install the
command for use. If there is already a Python command with this name
installed, the currently installed command will be uninstalled, and
this command installed in its stead.

The following code snippet shows how some trivial MI commands can be
implemented in Python:

```
class MIEcho(gdb.MICommand):
    """Echo arguments passed to the command."""

    def __init__(self, name, mode):
        self._mode = mode
        super(MIEcho, self).__init__(name)

    def invoke(self, argv):
        if self._mode == 'dict':
            return { 'dict': { 'argv' : argv } }
        elif self._mode == 'list':
            return { 'list': argv }
        else:
            return { 'string': ", ".join(argv) }

MIEcho("-echo-dict", "dict")
MIEcho("-echo-list", "list")
MIEcho("-echo-string", "string")
```

The last three lines instantiate the class three times, creating three
new GDB/MI commands `-echo-dict`, `-echo-list`, and
`-echo-string`. Each time a subclass of `gdb.MICommand` is
instantiated, the new command is automatically registered with
GDB.

Depending on how the Python code is read into GDB, you may
need to import the `gdb` module explicitly.

The following example shows a GDB session in which the above
commands have been added:

```
(gdb)
-echo-dict abc def ghi
^done,dict={argv=["abc","def","ghi"]}
(gdb)
-echo-list abc def ghi
^done,list=["abc","def","ghi"]
(gdb)
-echo-string abc def ghi
^done,string="abc, def, ghi"
(gdb)
```

Conversely, it is possible to execute GDB/MI commands from
Python, with the results being a Python object and not a
specially-formatted string. This is done with the
`gdb.execute_mi` function.

Function: **gdb.execute\_mi** _(command \[, arg \]…)_

Invoke a GDB/MI command. command is the name of the
command, a string. The arguments, arg, are passed to the
command. Each argument must also be a string.

This function returns a Python dictionary whose contents reflect the
corresponding GDB/MI command’s output. Refer to the
documentation for these commands for details. Lists are represented
as Python lists, and tuples are represented as Python dictionaries.

If the command fails, it will raise a Python exception.

Here is how this works using the commands from the example above:

```
(gdb) python print(gdb.execute_mi("-echo-dict", "abc", "def", "ghi"))
{'dict': {'argv': ['abc', 'def', 'ghi']}}
(gdb) python print(gdb.execute_mi("-echo-list", "abc", "def", "ghi"))
{'list': ['abc', 'def', 'ghi']}
(gdb) python print(gdb.execute_mi("-echo-string", "abc", "def", "ghi"))
{'string': 'abc, def, ghi'}
```

## 23.3.2.23 GDB/MI Notifications In Python

It is possible to emit GDB/MI notifications from
Python. Use the `gdb.notify_mi` function to do that.

Function: **gdb.notify\_mi** _(name \[, data\])_

Emit a GDB/MI asynchronous notification. name is the name of the
notification, consisting of alphanumeric characters and a hyphen (`-`).
data is any additional data to be emitted with the notification, passed
as a Python dictionary. This argument is optional. The dictionary is converted
to a GDB/MIresult records (see [GDB/MI Output Syntax](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Output-Syntax.html#GDB_002fMI-Output-Syntax)) the same way
as result of Python MI command (see [GDB/MI Commands In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Commands-In-Python.html#GDB_002fMI-Commands-In-Python)).

If data is `None` then no additional values are emitted.

While using existing notification names (see [GDB/MI Async Records](https://sourceware.org/gdb/current/onlinedocs/gdb.html/GDB_002fMI-Async-Records.html#GDB_002fMI-Async-Records)) with
`gdb.notify_mi` is allowed, users are encouraged to prefix user-defined
notification with a hyphen (`-`) to avoid possible conflict.
GDB will never introduce notification starting with hyphen.

Here is how to emit `=-connection-removed` whenever a connection to remote
GDB server is closed (see [Connections In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Connections-In-Python.html#Connections-In-Python)):

```
def notify_connection_removed(event):
    data = {"id": event.connection.num, "type": event.connection.type}
    gdb.notify_mi("-connection-removed", data)

gdb.events.connection_removed.connect(notify_connection_removed)
```

Then, each time a connection is closed, there will be a notification on MI channel:

```
=-connection-removed,id="1",type="remote"
```

## 23.3.2.24 Parameters In Python

You can implement new GDB parameters using Python. A new
parameter is implemented as an instance of the `gdb.Parameter`
class.

Parameters are exposed to the user via the `set` and
`show` commands. See [Help](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Help.html#Help).

There are many parameters that already exist and can be set in
GDB. Two examples are: `set follow fork` and
`set charset`. Setting these parameters influences certain
behavior in GDB. Similarly, you can define parameters that
can be used to influence behavior in custom Python scripts and commands.

Function: **Parameter.\_\_init\_\_** _(name, command\_class, parameter\_class \[, enum\_sequence\])_

The object initializer for `Parameter` registers the new
parameter with GDB. This initializer is normally invoked
from the subclass’ own `__init__` method.

name is the name of the new parameter. If name consists
of multiple words, then the initial words are looked for as prefix
parameters. An example of this can be illustrated with the
`set print` set of parameters. If name is
`print foo`, then `print` will be searched as the prefix
parameter. In this case the parameter can subsequently be accessed in
GDB as `set print foo`.

If name consists of multiple words, and no prefix parameter group
can be found, an exception is raised.

command\_class should be one of the ‘ `COMMAND_`’ constants
(see [CLI Commands In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/CLI-Commands-In-Python.html#CLI-Commands-In-Python)). This argument tells GDB how to
categorize the new parameter in the help system.

parameter\_class should be one of the ‘ `PARAM_`’ constants
defined below. This argument tells GDB the type of the new
parameter; this information is used for input validation and
completion.

If parameter\_class is `PARAM_ENUM`, then
enum\_sequence must be a sequence of strings. These strings
represent the possible values for the parameter.

If parameter\_class is not `PARAM_ENUM`, then the presence
of a fourth argument will cause an exception to be thrown.

The help text for the new parameter includes the Python documentation
string from the parameter’s class, if there is one. If there is no
documentation string, a default value is used. The documentation
string is included in the output of the parameters `help set` and
`help show` commands, and should be written taking this into
account. If the documentation string for the parameter’s class is the
empty string then GDB will only use `Parameter.set_doc`
or `Parameter.show_doc` (see below) in the `help` output.

Variable: **Parameter.set\_doc**

If this attribute exists, and is a string, then its value is used as
the first part of the help text for this parameter’s `set`
command. The second part of the help text is taken from the
documentation string for the parameter’s class, if there is one.

The value of `set_doc` should give a brief summary specific to
the set action, this text is only displayed when the user runs the
`help set` command for this parameter. The class documentation
should be used to give a fuller description of what the parameter
does, this text is displayed for both the `help set` and
`help show` commands.

The `set_doc` value is examined when `Parameter.__init__` is
invoked; subsequent changes have no effect.

Variable: **Parameter.show\_doc**

If this attribute exists, and is a string, then its value is used as
the first part of the help text for this parameter’s `show`
command. The second part of the help text is taken from the
documentation string for the parameter’s class, if there is one.

The value of `show_doc` should give a brief summary specific to
the show action, this text is only displayed when the user runs the
`help show` command for this parameter. The class documentation
should be used to give a fuller description of what the parameter
does, this text is displayed for both the `help set` and
`help show` commands.

The `show_doc` value is examined when `Parameter.__init__`
is invoked; subsequent changes have no effect.

Variable: **Parameter.value**

The `value` attribute holds the underlying value of the
parameter. It can be read and assigned to just as any other
attribute. GDB does validation when assignments are made.

There are two methods that may be implemented in any `Parameter`
class. These are:

Function: **Parameter.get\_set\_string** _(self)_

If this method exists, GDB will call it when a
parameter’s value has been changed via the `set` API (for
example, `set foo off`). The `value` attribute has already
been populated with the new value and may be used in output. This
method must return a string. If the returned string is not empty,
GDB will present it to the user.

If this method raises the `gdb.GdbError` exception
(see [Exception Handling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Exception-Handling.html#Exception-Handling)), then GDB will print the
exception’s string and the `set` command will fail. Note,
however, that the `value` attribute will not be reset in this
case. So, if your parameter must validate values, it should store the
old value internally and reset the exposed value, like so:

```
class ExampleParam (gdb.Parameter):
   def __init__ (self, name):
      super (ExampleParam, self).__init__ (name,
                   gdb.COMMAND_DATA,
                   gdb.PARAM_BOOLEAN)
      self.value = True
      self.saved_value = True
   def validate(self):
      return False
   def get_set_string (self):
      if not self.validate():
        self.value = self.saved_value
        raise gdb.GdbError('Failed to validate')
      self.saved_value = self.value
      return ""
```

Function: **Parameter.get\_show\_string** _(self, svalue)_

GDB will call this method when a parameter’s
`show` API has been invoked (for example, `show foo`). The
argument `svalue` receives the string representation of the
current value. This method must return a string.

When a new parameter is defined, its type must be specified. The
available types are represented by constants defined in the `gdb`
module:

`gdb.PARAM_BOOLEAN`

The value is a plain boolean. The Python boolean values, `True`
and `False` are the only valid values.

`gdb.PARAM_AUTO_BOOLEAN`

The value has three possible states: true, false, and ‘ `auto`’. In
Python, true and false are represented using boolean constants, and
‘ `auto`’ is represented using `None`.

`gdb.PARAM_UINTEGER`

The value is an unsigned integer. The value of `None` should be
interpreted to mean “unlimited” (literal `'unlimited'` can also
be used to set that value), and the value of 0 is reserved and should
not be used.

`gdb.PARAM_INTEGER`

The value is a signed integer. The value of `None` should be
interpreted to mean “unlimited” (literal `'unlimited'` can also
be used to set that value), and the value of 0 is reserved and should
not be used.

`gdb.PARAM_STRING`

The value is a string. When the user modifies the string, any escape
sequences, such as ‘ `\t`’, ‘ `\f`’, and octal escapes, are
translated into corresponding characters and encoded into the current
host charset.

`gdb.PARAM_STRING_NOESCAPE`

The value is a string. When the user modifies the string, escapes are
passed through untranslated.

`gdb.PARAM_OPTIONAL_FILENAME`

The value is a either a filename (a string), or `None`.

`gdb.PARAM_FILENAME`

The value is a filename. This is just like
`PARAM_STRING_NOESCAPE`, but uses file names for completion.

`gdb.PARAM_ZINTEGER`

The value is a signed integer. This is like `PARAM_INTEGER`,
except that 0 is allowed and the value of `None` is not supported.

`gdb.PARAM_ZUINTEGER`

The value is an unsigned integer. This is like `PARAM_UINTEGER`,
except that 0 is allowed and the value of `None` is not supported.

`gdb.PARAM_ZUINTEGER_UNLIMITED`

The value is a signed integer. This is like `PARAM_INTEGER`
including that the value of `None` should be interpreted to mean
“unlimited” (literal `'unlimited'` can also be used to set that
value), except that 0 is allowed, and the value cannot be negative,
except the special value -1 is returned for the setting of “unlimited”.

`gdb.PARAM_ENUM`

The value is a string, which must be one of a collection string
constants provided when the parameter is created.

`gdb.PARAM_COLOR`

The value is `gdb.Color` instance.

When creating multiple new parameters using `gdb.Parameter`, it
is often desirable to create a prefix command that can be used to
group related parameters together, for example, if you wished to add
the parameters `plugin-name feature-1` and `plugin-name
feature-2`, then the `plugin-name` would need to be a prefix
command (see [CLI Commands In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/CLI-Commands-In-Python.html#CLI-Commands-In-Python)).

However, when creating parameters, you will almost always need to
create two prefix commands, one as a `set` sub-command, and one as
a `show` sub-command. GDB provides the
`gdb.ParameterPrefix` helper class to make creation of these two
prefixes easier.

Function: **ParameterPrefix.\_\_init\_\_** _(name, command\_class, doc = `None`)_

The object initializer for `ParameterPrefix` registers two new
`gdb.Command` prefixes, one as a `set` sub-command, and the
other as a `show` sub-command.

name, a string, is the name of the new prefix, without either
`set` or `show`, similar to the name passed to
`gdb.Parameter` (see [Parameters In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Parameters-In-Python.html#Parameters-In-Python)). For example, to
create the prefixes `set plugin-name` and `show plugin-name`,
you would pass the string `plugin-name`.

command\_class should be one of the ‘ `COMMAND_`’ constants
(see [CLI Commands In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/CLI-Commands-In-Python.html#CLI-Commands-In-Python)). This argument tells GDB how to
categorize the new parameter prefixes in the help system.

There are a number of ways in which the help text for the two new
prefix commands can be provided. If the doc parameter is not
`None`, then this will be used as the documentation string for
both prefix commands.

If doc is `None`, but `gdb.ParameterPrefix` has been
sub-classed, then the prefix command documentation will be taken from
sub-classes documentation string (i.e., the `__doc__` attribute).

If doc is `None`, and there is no `__doc__` string,
then the default value ‘ `This command is not documented.`’ is used.

When writing the help text, keep in mind that the same text is used
for both the `set` and `show` prefix commands.

Function: **ParameterPrefix.invoke\_set** _(argument, from\_tty)_

If a sub-class defines this method, then GDB will call this
when the prefix command is used with an unknown sub-command. The
argument and from\_tty parameters are the same as for
`gdb.Command.invoke` (see [Command.invoke](https://sourceware.org/gdb/current/onlinedocs/gdb.html/CLI-Commands-In-Python.html#Command_002einvoke)).

If this method throws an exception, it is turned into a GDB`error` call. Otherwise, the return value is ignored.

It is not required that a `ParameterPrefix` sub-class override
this method. Usually, a parameter prefix only exists as a means to
group related parameters together. GDB handles this use case
automatically with no need to implement `invoke_set`.

Function: **ParameterPrefix.invoke\_show** _(argument, from\_tty)_

This is like the `invoke_set` method, but for the `show`
prefix command. As with `invoke_set`, implementation of this
method is optional, and usually not required.

Function: **ParameterPrefix.dont\_repeat** _()_

Like `Command.dont_repeat` (see [Command.dont\_repeat](https://sourceware.org/gdb/current/onlinedocs/gdb.html/CLI-Commands-In-Python.html#Command_002edont_005frepeat)), this
can be called from `ParameterPrefix.invoke_set` or
`ParameterPrefix.invoke_show` to prevent the prefix commands from
being repeated.

Here is a small example that uses `gdb.ParameterPrefix` along
with `gdb.Parameter` to create two new parameters
`plugin-name feature-1` and `plugin-name feature-2`. As
neither `invoke_set` or `invoke_show` is needed, this
example does not sub-class `gdb.ParameterPrefix`:

```
class ExampleParam(gdb.Parameter):
   def __init__ (self, name):
      super ().__init__ (name, gdb.COMMAND_DATA, gdb.PARAM_BOOLEAN)
      self.value = True

gdb.ParameterPrefix("plugin-name", gdb.COMMAND_NONE,
                    """An example parameter prefix.

                    This groups together some parameters.""")
ExampleParam("plugin-name feature-1")
ExampleParam("plugin-name feature-2")
```

The helper class `gdb.StyleParameterSet` exists to make it easier
to create new styles. GDB places style settings under
‘ `show style …`’ and ‘ `set style …`’, an example of a style
is ‘ `filename`’. Each style is really a prefix command (see [CLI Commands In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/CLI-Commands-In-Python.html#CLI-Commands-In-Python)), with sub-commands ‘ `foreground`’,
‘ `background`’, and optionally, ‘ `intensity`’.

It is simple enough to create a new style using two `gdb.Command`
objects for the prefix commands (one for ‘ `set`’, and one for
‘ `show`’), and three `gdb.Parameter` objects, one each for the
‘ `foreground`’, ‘ `background`’, and ‘ `intensity`’. You would
also want to take care to craft the help text so that the new style
behaves the same as the existing styles.

Or, you can use the `gdb.StyleParameterSet` class, which takes
care of all this, as the following example shows:

```
(gdb) python s = gdb.StyleParameterSet("my-style")
(gdb) show style my-style
style my-style background:  The "my-style" style background color is: none
style my-style foreground:  The "my-style" style foreground color is: none
style my-style intensity:  The "my-style" style display intensity is: normal
(gdb)
```

You might also want to group a number of styles within a new prefix,
similar to how GDB groups disassembler related styles within
the ‘ `style disassembler`’ prefix. This can be done using
`gdb.ParameterPrefix` (see [gdb.ParameterPrefix](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Parameters-In-Python.html#gdb_002eParameterPrefix)), as in this
example:

```
(gdb) python gdb.ParameterPrefix("style group", gdb.COMMAND_NONE)
(gdb) python s_a = gdb.StyleParameterSet("group aa")
(gdb) python s_b = gdb.StyleParameterSet("group bb")
(gdb) show style group
style group aa background:  The "group aa" style background color is: none
style group aa foreground:  The "group aa" style foreground color is: none
style group aa intensity:  The "group aa" style display intensity is: normal
style group bb background:  The "group bb" style background color is: none
style group bb foreground:  The "group bb" style foreground color is: none
style group bb intensity:  The "group bb" style display intensity is: normal
(gdb)
```

The `gdb.StyleParameterSet` class has the following methods and
attributes:

Function: **StyleParameterSet.\_\_init\_\_(name,** _add\_intensity=`True`, doc=`None`)_

Create a new style group based on name, which is a string. For
example if name is ‘ `my-style`’, then GDB will
create the prefix commands ‘ `set style my-style`’ and ‘ `show
style my-style`’. Within these prefix commands will be
‘ `foreground`’, ‘ `background`’, and ‘ `intensity`’ parameters
with the appropriate types.

It is also possible for name to consist of multiple words, so
long as each prefix command (except the last one) already exists. For
example, it is valid to use a name value of ‘ `disassembler
my-style`’, as the ‘ `disassembler`’ prefix command already exists.
GDB would then create ‘ `set style disassembler my-style`’
and ‘ `show style disassembler my-style`’, and within the
‘ `my-style`’ prefixes will be the ‘ `foreground`’,
‘ `background`’, and ‘ `intensity`’ parameters with the
appropriate types.

Every style requires a ‘ `foreground`’ and ‘ `background`’, but
not every style needs an ‘ `intensity`’. If add\_intensity is
`True` (the default), then the ‘ `intensity`’ parameter will be
created. If add\_intensity is `False`, then the
‘ `intensity`’ parameter will not be created.

If the ‘ `intensity`’ parameter is not created, then the
`gdb.Style` (see [Styles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Styles-In-Python.html#Styles-In-Python)) created from this
`gdb.StyleParameterSet` will have `gdb.INTENSITY_NORMAL`.

The doc should be a string which will be used as the help text
for the name prefix command. This text is used as the
`Command.__doc__` value for the `gdb.Command` object that is
the prefix command object (see [CLI Commands In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/CLI-Commands-In-Python.html#CLI-Commands-In-Python)). If
doc is `None` (the default) then a basic default help text
is used.

Function: **StyleParameterSet.apply(string)**

Equivalent to `StyleParameterSet.style.apply(string)`. Returns a
copy of string with escape sequences added to the start and end.
The escape sequence at the start applies this style, and the escape
sequence at the end restores the terminal default.

If styling is disabled (i.e. ‘ `set style enabled off`’), then no
escape sequences are added and this method returns a copy of
string.

Variable: **StyleParameterSet.style**

This read/write attribute holds a `gdb.Style` object
(see [Styles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Styles-In-Python.html#Styles-In-Python)), that is a named style associated with this
style parameter group.

Variable: **StyleParameterSet.value**

This is an alias for `StyleParameterSet.style`, see above.

## 23.3.2.25 Writing new convenience functions

You can implement new convenience functions (see [Convenience Vars](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Convenience-Vars.html#Convenience-Vars))
in Python. A convenience function is an instance of a subclass of the
class `gdb.Function`.

Function: **Function.\_\_init\_\_** _(name)_

The initializer for `Function` registers the new function with
GDB. The argument name is the name of the function,
a string. The function will be visible to the user as a convenience
variable of type `internal function`, whose name is the same as
the given name.

The documentation for the new function is taken from the documentation
string for the new class.

Function: **Function.invoke** _(\*args)_

When a convenience function is evaluated, its arguments are converted
to instances of `gdb.Value`, and then the function’s
`invoke` method is called. Note that GDB does not
predetermine the arity of convenience functions. Instead, all
available arguments are passed to `invoke`, following the
standard Python calling convention. In particular, a convenience
function can have default values for parameters without ill effect.

The return value of this method is used as its value in the enclosing
expression. If an ordinary Python value is returned, it is converted
to a `gdb.Value` following the usual rules.

The following code snippet shows how a trivial convenience function can
be implemented in Python:

```
class Greet (gdb.Function):
  """Return string to greet someone.
Takes a name as argument."""

  def __init__ (self):
    super (Greet, self).__init__ ("greet")

  def invoke (self, name):
    return "Hello, %s!" % name.string ()

Greet ()
```

The last line instantiates the class, and is necessary to trigger the
registration of the function with GDB. Depending on how the
Python code is read into GDB, you may need to import the
`gdb` module explicitly.

Now you can use the function in an expression:

```
(gdb) print $greet("Bob")
$1 = "Hello, Bob!"
```

## 23.3.2.26 Program Spaces In Python

A program space, or _progspace_, represents a symbolic view
of an address space.
It consists of all of the objfiles of the program.
See [Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfiles-In-Python).
See [program spaces](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-Connections-and-Programs.html#Inferiors-Connections-and-Programs), for more details
about program spaces.

The following progspace-related functions are available in the
`gdb` module:

Function: **gdb.current\_progspace** _()_

This function returns the program space of the currently selected inferior.
See [Inferiors Connections and Programs](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-Connections-and-Programs.html#Inferiors-Connections-and-Programs). This is identical to
`gdb.selected_inferior().progspace` (see [Inferiors In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#Inferiors-In-Python)) and is
included for historical compatibility.

Function: **gdb.progspaces** _()_

Return a sequence of all the progspaces currently known to GDB.

Each progspace is represented by an instance of the `gdb.Progspace`
class.

Variable: **Progspace.filename**

The file name, as a string, of the main symbol file (from which debug
symbols have been loaded) for the progspace, e.g. the argument to
the `symbol-file` or `file` commands.

If there is no main symbol table currently loaded, then this attribute
will be `None`.

Variable: **Progspace.symbol\_file**

The `gdb.Objfile` representing the main symbol file (from which
debug symbols have been loaded) for the `gdb.Progspace`. This is
the symbol file set by the `symbol-file` or `file` commands.

This will be the `gdb.Objfile` representing
`Progspace.filename` when `Progspace.filename` is not
`None`.

If there is no main symbol table currently loaded, then this attribute
will be `None`.

If the `Progspace` is invalid, i.e., when
`Progspace.is_valid()` returns `False`, then attempting to
access this attribute will raise a `RuntimeError` exception.

Variable: **Progspace.executable\_filename**

The file name, as a string, of the executable file in use by this
program space. The executable file is the file that GDB will
invoke in order to start an inferior when using a native target. The
file name within this attribute is updated by the `exec-file` and
`file` commands.

If no executable is currently set within this `Progspace` then
this attribute contains `None`.

If the `Progspace` is invalid, i.e., when
`Progspace.is_valid()` returns `False`, then attempting to
access this attribute will raise a `RuntimeError` exception.

Variable: **Progspace.pretty\_printers**

The `pretty_printers` attribute is a list of functions. It is
used to look up pretty-printers. A `Value` is passed to each
function in order; if the function returns `None`, then the
search continues. Otherwise, the return value should be an object
which is used to format the value. See [Pretty Printing API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Pretty-Printing-API.html#Pretty-Printing-API), for more
information.

Variable: **Progspace.type\_printers**

The `type_printers` attribute is a list of type printer objects.
See [Type Printing API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Type-Printing-API.html#Type-Printing-API), for more information.

Variable: **Progspace.frame\_filters**

The `frame_filters` attribute is a dictionary of frame filter
objects. See [Frame Filter API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frame-Filter-API.html#Frame-Filter-API), for more information.

Variable: **Progspace.missing\_file\_handlers**

The `missing_file_handlers` attribute is a list of tuples. Each
tuple holds a missing-file handler object for this program space. For
more information, see [Missing Debug Info In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Missing-Debug-Info-In-Python.html#Missing-Debug-Info-In-Python), and
[Missing Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Missing-Objfiles-In-Python.html#Missing-Objfiles-In-Python).

A program space has the following methods:

Function: **Progspace.block\_for\_pc** _(pc)_

Return the innermost `gdb.Block` containing the given pc
value. If the block cannot be found for the pc value specified,
the function will return `None`.

Function: **Progspace.find\_pc\_line** _(pc)_

Return the `gdb.Symtab_and_line` object corresponding to the
pc value. See [Symbol Tables In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbol-Tables-In-Python.html#Symbol-Tables-In-Python). If an invalid value
of pc is passed as an argument, then the `symtab` and
`line` attributes of the returned `gdb.Symtab_and_line`
object will be `None` and 0 respectively.

Function: **Progspace.is\_valid** _()_

Returns `True` if the `gdb.Progspace` object is valid,
`False` if not. A `gdb.Progspace` object can become invalid
if the program space file it refers to is not referenced by any
inferior. All other `gdb.Progspace` methods will throw an
exception if it is invalid at the time the method is called.

Function: **Progspace.objfiles** _()_

Return a sequence of all the objfiles referenced by this program
space. See [Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfiles-In-Python).

Function: **Progspace.solib\_name** _(address)_

Return the name of the shared library holding the given address
as a string, or `None`.

Function: **Progspace.objfile\_for\_address** _(address)_

Return the `gdb.Objfile` holding the given address, or
`None` if no objfile covers it.

One may add arbitrary attributes to `gdb.Progspace` objects
in the usual Python way.
This is useful if, for example, one needs to do some extra record keeping
associated with the program space.

See [choosing attribute names](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#choosing-attribute-names), for guidance on selecting a suitable
name for new attributes.

In this contrived example, we want to perform some processing when
an objfile with a certain symbol is loaded, but we only want to do
this once because it is expensive. To achieve this we record the results
with the program space because we can’t predict when the desired objfile
will be loaded.

```
(gdb) python
```

```
def clear_objfiles_handler(event):
    event.progspace.expensive_computation = None
def expensive(symbol):
    """A mock routine to perform an "expensive" computation on symbol."""
    print ("Computing the answer to the ultimate question ...")
    return 42
```

```
def new_objfile_handler(event):
    objfile = event.new_objfile
    progspace = objfile.progspace
    if not hasattr(progspace, 'expensive_computation') or \
            progspace.expensive_computation is None:
        # We use 'main' for the symbol to keep the example simple.
        # Note: There's no current way to constrain the lookup
        # to one objfile.
        symbol = gdb.lookup_global_symbol('main')
        if symbol is not None:
            progspace.expensive_computation = expensive(symbol)
gdb.events.clear_objfiles.connect(clear_objfiles_handler)
gdb.events.new_objfile.connect(new_objfile_handler)
end
```

```
(gdb) file /tmp/hello
Reading symbols from /tmp/hello...
Computing the answer to the ultimate question ...
(gdb) python print(gdb.current_progspace().expensive_computation)
42
(gdb) run
Starting program: /tmp/hello
Hello.
[Inferior 1 (process 4242) exited normally]
```

## 23.3.2.27 Objfiles In Python

GDB loads symbols for an inferior from various
symbol-containing files (see [Files](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Files.html#Files)). These include the primary
executable file, any shared libraries used by the inferior, and any
separate debug info files (see [Separate Debug Files](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Separate-Debug-Files.html#Separate-Debug-Files)).
GDB calls these symbol-containing files _objfiles_.

The following objfile-related functions are available in the
`gdb` module:

Function: **gdb.current\_objfile** _()_

When auto-loading a Python script (see [Python Auto-loading](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Python-Auto_002dloading.html#Python-Auto_002dloading)), GDB
sets the “current objfile” to the corresponding objfile. This
function returns the current objfile. If there is no current objfile,
this function returns `None`.

Function: **gdb.objfiles** _()_

Return a sequence of objfiles referenced by the current program space.
See [Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfiles-In-Python), and [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python). This is identical
to `gdb.selected_inferior().progspace.objfiles()` and is included for
historical compatibility.

Function: **gdb.lookup\_objfile** _(name \[, by\_build\_id\])_

Look up name, a file name or build ID, in the list of objfiles
for the current program space (see [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python)).
If the objfile is not found throw the Python `ValueError` exception.

If name is a relative file name, then it will match any
source file name with the same trailing components. For example, if
name is ‘ `gcc/expr.c`’, then it will match source file
name of `/build/trunk/gcc/expr.c`, but not
`/build/trunk/libcpp/expr.c` or `/build/trunk/gcc/x-expr.c`.

If by\_build\_id is provided and is `True` then name
is the build ID of the objfile. Otherwise, name is a file name.
This is supported only on some operating systems, notably those which use
the ELF format for binary files and the GNU Binutils. For more details
about this feature, see the description of the `--build-id`
command-line option in [Command Line Options](http://sourceware.org/binutils/docs/ld/Options.html#Options) in The GNU Linker.

Each objfile is represented by an instance of the `gdb.Objfile`
class.

Variable: **Objfile.filename**

The file name of the objfile as a string, with symbolic links resolved.

The value is `None` if the objfile is no longer valid.
See the `gdb.Objfile.is_valid` method, described below.

Variable: **Objfile.username**

The file name of the objfile as specified by the user as a string.

The value is `None` if the objfile is no longer valid.
See the `gdb.Objfile.is_valid` method, described below.

Variable: **Objfile.is\_file**

An objfile often comes from an ordinary file, but in some cases it may
be constructed from the contents of memory. This attribute is
`True` for file-backed objfiles, and `False` for other
kinds.

Variable: **Objfile.owner**

For separate debug info objfiles this is the corresponding `gdb.Objfile`
object that debug info is being provided for.
Otherwise this is `None`.
Separate debug info objfiles are added with the
`gdb.Objfile.add_separate_debug_file` method, described below.

Variable: **Objfile.build\_id**

The build ID of the objfile as a string.
If the objfile does not have a build ID then the value is `None`.

This is supported only on some operating systems, notably those which use
the ELF format for binary files and the GNU Binutils. For more details
about this feature, see the description of the `--build-id`
command-line option in [Command Line Options](http://sourceware.org/binutils/docs/ld/Options.html#Options) in The GNU Linker.

Variable: **Objfile.progspace**

The containing program space of the objfile as a `gdb.Progspace`
object. See [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python).

Variable: **Objfile.pretty\_printers**

The `pretty_printers` attribute is a list of functions. It is
used to look up pretty-printers. A `Value` is passed to each
function in order; if the function returns `None`, then the
search continues. Otherwise, the return value should be an object
which is used to format the value. See [Pretty Printing API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Pretty-Printing-API.html#Pretty-Printing-API), for more
information.

Variable: **Objfile.type\_printers**

The `type_printers` attribute is a list of type printer objects.
See [Type Printing API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Type-Printing-API.html#Type-Printing-API), for more information.

Variable: **Objfile.frame\_filters**

The `frame_filters` attribute is a dictionary of frame filter
objects. See [Frame Filter API](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frame-Filter-API.html#Frame-Filter-API), for more information.

One may add arbitrary attributes to `gdb.Objfile` objects
in the usual Python way.
This is useful if, for example, one needs to do some extra record keeping
associated with the objfile.

See [choosing attribute names](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#choosing-attribute-names), for guidance on selecting a suitable
name for new attributes.

In this contrived example we record the time when GDB
loaded the objfile.

```
(gdb) python
import datetime
def new_objfile_handler(event):
    # Set the time_loaded attribute of the new objfile.
    event.new_objfile.time_loaded = datetime.datetime.today()
gdb.events.new_objfile.connect(new_objfile_handler)
end
```

```
(gdb) file ./hello
Reading symbols from ./hello...
(gdb) python print(gdb.objfiles()[0].time_loaded)
2014-10-09 11:41:36.770345
```

A `gdb.Objfile` object has the following methods:

Function: **Objfile.is\_valid** _()_

Returns `True` if the `gdb.Objfile` object is valid,
`False` if not. A `gdb.Objfile` object can become invalid
if the object file it refers to is not loaded in GDB any
longer. All other `gdb.Objfile` methods will throw an exception
if it is invalid at the time the method is called.

Function: **Objfile.add\_separate\_debug\_file** _(file)_

Add file to the list of files that GDB will search for
debug information for the objfile.
This is useful when the debug info has been removed from the program
and stored in a separate file. GDB has built-in support for
finding separate debug info files (see [Separate Debug Files](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Separate-Debug-Files.html#Separate-Debug-Files)), but if
the file doesn’t live in one of the standard places that GDB
searches then this function can be used to add a debug info file
from a different place.

Function: **Objfile.lookup\_global\_symbol** _(name \[, domain\])_

Search for a global symbol named name in this objfile. Optionally, the
search scope can be restricted with the domain argument.
The domain argument must be a domain constant defined in the `gdb`
module and described in [Symbols In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbols-In-Python.html#Symbols-In-Python). This function is similar to
`gdb.lookup_global_symbol`, except that the search is limited to this
objfile.

The result is a `gdb.Symbol` object or `None` if the symbol
is not found.

Function: **Objfile.lookup\_static\_symbol** _(name \[, domain\])_

Like `Objfile.lookup_global_symbol`, but searches for a global
symbol with static linkage named name in this objfile.

## 23.3.2.28 Accessing inferior stack frames from Python

When the debugged program stops, GDB is able to analyze its call
stack (see [Stack frames](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frames.html#Frames)). The `gdb.Frame` class
represents a frame in the stack. A `gdb.Frame` object is only valid
while its corresponding frame exists in the inferior’s stack. If you try
to use an invalid frame object, GDB will throw a `gdb.error`
exception (see [Exception Handling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Exception-Handling.html#Exception-Handling)).

Two `gdb.Frame` objects can be compared for equality with the `==`
operator, like:

```
(gdb) python print(gdb.newest_frame() == gdb.selected_frame ())
True
```

The following frame-related functions are available in the `gdb` module:

Function: **gdb.selected\_frame** _()_

Return the selected frame object. (see [Selecting a Frame](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Selection.html#Selection)).

Function: **gdb.newest\_frame** _()_

Return the newest frame object for the selected thread.

Function: **gdb.frame\_stop\_reason\_string** _(reason)_

Return a string explaining the reason why GDB stopped unwinding
frames, as expressed by the given reason code (an integer, see the
`unwind_stop_reason` method further down in this section).

Function: **gdb.invalidate\_cached\_frames**

GDB internally keeps a cache of the frames that have been
unwound. This function invalidates this cache.

This function should not generally be called by ordinary Python code.
It is documented for the sake of completeness.

A `gdb.Frame` object has the following methods:

Function: **Frame.is\_valid** _()_

Returns true if the `gdb.Frame` object is valid, false if not.
A frame object can become invalid if the frame it refers to doesn’t
exist anymore in the inferior. All `gdb.Frame` methods will throw
an exception if it is invalid at the time the method is called.

Function: **Frame.name** _()_

Returns the function name of the frame, or `None` if it can’t be
obtained.

Function: **Frame.architecture** _()_

Returns the `gdb.Architecture` object corresponding to the frame’s
architecture. See [Architectures In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Architectures-In-Python.html#Architectures-In-Python).

Function: **Frame.type** _()_

Returns the type of the frame. The value can be one of:

`gdb.NORMAL_FRAME`

An ordinary stack frame.

`gdb.DUMMY_FRAME`

A fake stack frame that was created by GDB when performing an
inferior function call.

`gdb.INLINE_FRAME`

A frame representing an inlined function. The function was inlined
into a `gdb.NORMAL_FRAME` that is older than this one.

`gdb.TAILCALL_FRAME`

A frame representing a tail call. See [Tail Call Frames](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Tail-Call-Frames.html#Tail-Call-Frames).

`gdb.SIGTRAMP_FRAME`

A signal trampoline frame. This is the frame created by the OS when
it calls into a signal handler.

`gdb.ARCH_FRAME`

A fake stack frame representing a cross-architecture call.

`gdb.SENTINEL_FRAME`

This is like `gdb.NORMAL_FRAME`, but it is only used for the
newest frame.

Function: **Frame.unwind\_stop\_reason** _()_

Return an integer representing the reason why it’s not possible to find
more frames toward the outermost frame. Use
`gdb.frame_stop_reason_string` to convert the value returned by this
function to a string. The value can be one of:

`gdb.FRAME_UNWIND_NO_REASON`

No particular reason (older frames should be available).

`gdb.FRAME_UNWIND_NULL_ID`

The previous frame’s analyzer returns an invalid result. This is no
longer used by GDB, and is kept only for backward
compatibility.

`gdb.FRAME_UNWIND_OUTERMOST`

This frame is the outermost.

`gdb.FRAME_UNWIND_UNAVAILABLE`

Cannot unwind further, because that would require knowing the
values of registers or memory that have not been collected.

`gdb.FRAME_UNWIND_INNER_ID`

This frame ID looks like it ought to belong to a NEXT frame,
but we got it for a PREV frame. Normally, this is a sign of
unwinder failure. It could also indicate stack corruption.

`gdb.FRAME_UNWIND_SAME_ID`

This frame has the same ID as the previous one. That means
that unwinding further would almost certainly give us another
frame with exactly the same ID, so break the chain. Normally,
this is a sign of unwinder failure. It could also indicate
stack corruption.

`gdb.FRAME_UNWIND_NO_SAVED_PC`

The frame unwinder did not find any saved PC, but we needed
one to unwind further.

`gdb.FRAME_UNWIND_MEMORY_ERROR`

The frame unwinder caused an error while trying to access memory.

`gdb.FRAME_UNWIND_FIRST_ERROR`

Any stop reason greater or equal to this value indicates some kind
of error. This special value facilitates writing code that tests
for errors in unwinding in a way that will work correctly even if
the list of the other values is modified in future GDB
versions. Using it, you could write:

```
reason = gdb.selected_frame().unwind_stop_reason ()
reason_str =  gdb.frame_stop_reason_string (reason)
if reason >=  gdb.FRAME_UNWIND_FIRST_ERROR:
    print ("An error occurred: %s" % reason_str)
```

Function: **Frame.pc** _()_

Returns the frame’s resume address.

Function: **Frame.block** _()_

Return the frame’s code block. See [Blocks In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Blocks-In-Python.html#Blocks-In-Python). If the frame
does not have a block – for example, if there is no debugging
information for the code in question – then this will throw an
exception.

Function: **Frame.function** _()_

Return the symbol for the function corresponding to this frame.
See [Symbols In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbols-In-Python.html#Symbols-In-Python).

Function: **Frame.older** _()_

Return the frame that called this frame. If this is the oldest frame,
return `None`.

Function: **Frame.newer** _()_

Return the frame called by this frame. If this is the newest frame,
return `None`.

Function: **Frame.find\_sal** _()_

Return the frame’s symtab and line object.
See [Symbol Tables In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbol-Tables-In-Python.html#Symbol-Tables-In-Python).

Function: **Frame.read\_register** _(register)_

Return the value of register in this frame. Returns a
`Gdb.Value` object. Throws an exception if register does
not exist. The register argument must be one of the following:

1. A string that is the name of a valid register (e.g., `'sp'` or
   `'rax'`).

2. A `gdb.RegisterDescriptor` object (see [Registers In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Registers-In-Python.html#Registers-In-Python)).

3. A GDB internal, platform specific number. Using these
   numbers is supported for historic reasons, but is not recommended as
   future changes to GDB could change the mapping between
   numbers and the registers they represent, breaking any Python code
   that uses the platform-specific numbers. The numbers are usually
   found in the corresponding `platform-tdep.h` file in the
   GDB source tree.


Using a string to access registers will be slightly slower than the
other two methods as GDB must look up the mapping between
name and internal register number. If performance is critical
consider looking up and caching a `gdb.RegisterDescriptor`
object.

Function: **Frame.read\_var** _(variable \[, block\])_

Return the value of variable in this frame. If the optional
argument block is provided, search for the variable from that
block; otherwise start at the frame’s current block (which is
determined by the frame’s current program counter). The variable
argument must be a string or a `gdb.Symbol` object; block must be a
`gdb.Block` object.

Function: **Frame.select** _()_

Set this frame to be the selected frame. See [Examining the\\
Stack](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Stack.html#Stack).

Function: **Frame.static\_link** _()_

In some languages (e.g., Ada, but also a GNU C extension), a nested
function can access the variables in the outer scope. This is done
via a “static link”, which is a reference from the nested frame to
the appropriate outer frame.

This method returns this frame’s static link frame, if one exists. If
there is no static link, this method returns `None`.

Function: **Frame.level** _()_

Return an integer, the stack frame level for this frame. See [Stack Frames](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frames.html#Frames).

Function: **Frame.language** _()_

Return a string, the source language for this frame.

## 23.3.2.29 Accessing blocks from Python

In GDB, symbols are stored in blocks. A block corresponds
roughly to a scope in the source code. Blocks are organized
hierarchically, and are represented individually in Python as a
`gdb.Block`. Blocks rely on debugging information being
available.

A frame has a block. Please see [Frames In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frames-In-Python.html#Frames-In-Python), for a more
in-depth discussion of frames.

The outermost block is known as the _global block_. The global
block typically holds public global variables and functions.

The block nested just inside the global block is the _static_
_block_. The static block typically holds file-scoped variables and
functions.

GDB provides a method to get a block’s superblock, but there
is currently no way to examine the sub-blocks of a block, or to
iterate over all the blocks in a symbol table (see [Symbol Tables In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbol-Tables-In-Python.html#Symbol-Tables-In-Python)).

Here is a short example that should help explain blocks:

```
/* This is in the global block.  */
int global;

/* This is in the static block.  */
static int file_scope;

/* 'function' is in the global block, and 'argument' is
   in a block nested inside of 'function'.  */
int function (int argument)
{
  /* 'local' is in a block inside 'function'.  It may or may
     not be in the same block as 'argument'.  */
  int local;

  {
     /* 'inner' is in a block whose superblock is the one holding
        'local'.  */
     int inner;

     /* If this call is expanded by the compiler, you may see
        a nested block here whose function is 'inline_function'
        and whose superblock is the one holding 'inner'.  */
     inline_function ();
  }
}
```

A `gdb.Block` is iterable. The iterator returns the symbols
(see [Symbols In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbols-In-Python.html#Symbols-In-Python)) local to the block. Python programs
should not assume that a specific block object will always contain a
given symbol, since changes in GDB features and
infrastructure may cause symbols move across blocks in a symbol
table. You can also use Python’s _dictionary syntax_ to access
variables in this block, e.g.:

```
symbol = some_block['variable']  # symbol is of type gdb.Symbol
```

The following block-related functions are available in the `gdb`
module:

Function: **gdb.block\_for\_pc** _(pc)_

Return the innermost `gdb.Block` containing the given pc
value. If the block cannot be found for the pc value specified,
the function will return `None`. This is identical to
`gdb.current_progspace().block_for_pc(pc)` and is included for
historical compatibility.

A `gdb.Block` object has the following methods:

Function: **Block.is\_valid** _()_

Returns `True` if the `gdb.Block` object is valid,
`False` if not. A block object can become invalid if the block it
refers to doesn’t exist anymore in the inferior. All other
`gdb.Block` methods will throw an exception if it is invalid at
the time the method is called. The block’s validity is also checked
during iteration over symbols of the block.

A `gdb.Block` object has the following attributes:

Variable: **Block.start**

The start address of the block. This attribute is not writable.

Variable: **Block.end**

One past the last address that appears in the block. This attribute
is not writable.

Variable: **Block.ranges**

A tuple representing address ranges of the block. Each range is represented
as a pair (two-element tuple) where the first element is the start of the
range and the second element is one past the last address that appears in
the range. The order of ranges is unspecified. Contiguous blocks have only
one range. This attribute is not writable.

Variable: **Block.function**

The name of the block represented as a `gdb.Symbol`. If the
block is not named, then this attribute holds `None`. This
attribute is not writable.

For ordinary function blocks, the superblock is the static block.
However, you should note that it is possible for a function block to
have a superblock that is not the static block – for instance this
happens for an inlined function.

Variable: **Block.superblock**

The block containing this block. If this parent block does not exist,
this attribute holds `None`. This attribute is not writable.

Variable: **Block.subblocks**

A list of blocks nested in this block. If there are no blocks nested,
this attribute holds an empty list. This attribute is not writable.

Variable: **Block.global\_block**

The global block associated with this block. This attribute is not
writable.

Variable: **Block.static\_block**

The static block associated with this block. This attribute is not
writable.

Variable: **Block.is\_global**

`True` if the `gdb.Block` object is a global block,
`False` if not. This attribute is not
writable.

Variable: **Block.is\_static**

`True` if the `gdb.Block` object is a static block,
`False` if not. This attribute is not writable.

## 23.3.2.30 Python representation of Symbols

GDB represents every variable, function and type as an
entry in a symbol table. See [Examining the Symbol Table](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbols.html#Symbols).
Similarly, Python represents these symbols in GDB with the
`gdb.Symbol` object.

The following symbol-related functions are available in the `gdb`
module:

Function: **gdb.lookup\_symbol** _(name \[, block \[, domain\]\])_

This function searches for a symbol by name. The search scope can be
restricted to the parameters defined in the optional domain and block
arguments.

name is the name of the symbol. It must be a string. The
optional block argument restricts the search to symbols visible
in that block. The block argument must be a
`gdb.Block` object. If omitted, the block for the current frame
is used. The optional domain argument restricts
the search to the domain type. The domain argument must be a
domain constant defined in the `gdb` module and described later
in this chapter.

The result is a tuple of two elements.
The first element is a `gdb.Symbol` object or `None` if the symbol
is not found.
If the symbol is found, the second element is `True` if the symbol
is a field of a method’s object (e.g., `this` in C `++`),
otherwise it is `False`.
If the symbol is not found, the second element is `False`.

Function: **gdb.lookup\_global\_symbol** _(name \[, domain\])_

This function searches for a global symbol by name.
The search scope can be restricted to by the domain argument.

name is the name of the symbol. It must be a string.
The optional domain argument restricts the search to the domain type.
The domain argument must be a domain constant defined in the `gdb`
module and described later in this chapter.

The result is a `gdb.Symbol` object or `None` if the symbol
is not found.

Function: **gdb.lookup\_static\_symbol** _(name \[, domain\])_

This function searches for a global symbol with static linkage by name.
The search scope can be restricted to by the domain argument.

name is the name of the symbol. It must be a string.
The optional domain argument restricts the search to the domain type.
The domain argument must be a domain constant defined in the `gdb`
module and described later in this chapter.

The result is a `gdb.Symbol` object or `None` if the symbol
is not found.

Note that this function will not find function-scoped static variables. To look
up such variables, iterate over the variables of the function’s
`gdb.Block` and check that `block.addr_class` is
`gdb.SYMBOL_LOC_STATIC`.

There can be multiple global symbols with static linkage with the same
name. This function will only return the first matching symbol that
it finds. Which symbol is found depends on where the program is
currently stopped, as GDB will first search for matching
symbols in the current compilation unit. If it finds no match, or if
there is no current compilation unit (perhaps because the program is
not running), then `gdb.lookup_static_symbol` will return an
unspecified matching symbol. To safely handle the case of multiple
static symbols with the same name, an application can use
`gdb.lookup_static_symbols`.

Function: **gdb.lookup\_static\_symbols** _(name \[, domain\])_

Similar to `gdb.lookup_static_symbol`, this function searches for
global symbols with static linkage by name, and optionally restricted
by the domain argument. However, this function returns a list of all
matching symbols found, not just the first one.

name is the name of the symbol. It must be a string.
The optional domain argument restricts the search to the domain type.
The domain argument must be a domain constant defined in the `gdb`
module and described later in this chapter.

The result is a list of `gdb.Symbol` objects which could be empty
if no matching symbols were found.

Note that this function will not find function-scoped static variables. To look
up such variables, iterate over the variables of the function’s
`gdb.Block` and check that `block.addr_class` is
`gdb.SYMBOL_LOC_STATIC`.

A `gdb.Symbol` object has the following attributes:

Variable: **Symbol.type**

The type of the symbol or `None` if no type is recorded.
This attribute is represented as a `gdb.Type` object.
See [Types In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Types-In-Python.html#Types-In-Python). This attribute is not writable.

Variable: **Symbol.symtab**

The symbol table in which the symbol appears. This attribute is
represented as a `gdb.Symtab` object. See [Symbol Tables In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbol-Tables-In-Python.html#Symbol-Tables-In-Python). This attribute is not writable.

Variable: **Symbol.line**

The line number in the source code at which the symbol was defined.
This is an integer.

Variable: **Symbol.name**

The name of the symbol as a string. This attribute is not writable.

Variable: **Symbol.linkage\_name**

The name of the symbol, as used by the linker (i.e., may be mangled).
This attribute is not writable.

Variable: **Symbol.print\_name**

The name of the symbol in a form suitable for output. This is either
`name` or `linkage_name`, depending on whether the user
asked GDB to display demangled or mangled names.

Variable: **Symbol.addr\_class**

The address class of the symbol. This classifies how to find the value
of a symbol. Each address class is a constant defined in the
`gdb` module and described later in this chapter.

Variable: **Symbol.domain**

The domain of the symbol. Each domain is a constant defined in the
`gdb` module and described later in this chapter.

Variable: **Symbol.needs\_frame**

This is `True` if evaluating this symbol’s value requires a frame
(see [Frames In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frames-In-Python.html#Frames-In-Python)) and `False` otherwise. Typically,
local variables will require a frame, but other symbols will not.

Variable: **Symbol.is\_argument**

`True` if the symbol is an argument of a function.

Variable: **Symbol.is\_artificial**

`True` if the symbol is artificial. An artificial symbol is one
that is introduced by the compiler.

Variable: **Symbol.is\_constant**

`True` if the symbol is a constant.

Variable: **Symbol.is\_function**

`True` if the symbol is a function or a method.

Variable: **Symbol.is\_variable**

`True` if the symbol is a variable, as opposed to something like
a function or type. Note that this also returns `False` for
arguments.

A `gdb.Symbol` object has the following methods:

Function: **Symbol.is\_valid** _()_

Returns `True` if the `gdb.Symbol` object is valid,
`False` if not. A `gdb.Symbol` object can become invalid if
the symbol it refers to does not exist in GDB any longer.
All other `gdb.Symbol` methods will throw an exception if it is
invalid at the time the method is called.

Function: **Symbol.value** _(\[frame\])_

Compute the value of the symbol, as a `gdb.Value`. For
functions, this computes the address of the function, cast to the
appropriate type. If the symbol requires a frame in order to compute
its value, then frame must be given. If frame is not
given, or if frame is invalid, then this method will throw an
exception.

The available domain categories in `gdb.Symbol` are represented
as constants in the `gdb` module:

`gdb.SYMBOL_UNDEF_DOMAIN`

This is used when a domain has not been discovered or none of the
following domains apply. This usually indicates an error either
in the symbol information or in GDB’s handling of symbols.

`gdb.SYMBOL_VAR_DOMAIN`

This domain contains variables.

`gdb.SYMBOL_FUNCTION_DOMAIN`

This domain contains functions.

`gdb.SYMBOL_TYPE_DOMAIN`

This domain contains types. In a C-like language, types using a tag
(the name appearing after a `struct`, `union`, or
`enum` keyword) will not appear here; in other languages, all
types are in this domain.

`gdb.SYMBOL_STRUCT_DOMAIN`

This domain holds struct, union and enum tag names. This domain is
only used for C-like languages. For example, in this code:

```
struct type_one { int x; };
typedef struct type_one type_two;
```

Here `type_one` will be in `SYMBOL_STRUCT_DOMAIN`, but
`type_two` will be in `SYMBOL_TYPE_DOMAIN`.

`gdb.SYMBOL_LABEL_DOMAIN`

This domain contains names of labels (for gotos).

`gdb.SYMBOL_MODULE_DOMAIN`

This domain contains names of Fortran module types.

`gdb.SYMBOL_COMMON_BLOCK_DOMAIN`

This domain contains names of Fortran common blocks.

When searching for a symbol, the desired domain constant can be passed
verbatim to the lookup function. For example:

```
symbol = gdb.lookup_symbol ("name", domain=gdb.SYMBOL_VAR_DOMAIN)
```

For more complex searches, there is a corresponding set of constants,
each named after one of the preceding constants, but with the
‘ `SEARCH`’ prefix replacing the ‘ `SYMBOL`’ prefix; for example,
`SEARCH_LABEL_DOMAIN`. These may be or’d together to form a
search constant, e.g.:

```
symbol = gdb.lookup_symbol ("name",
                            domain=gdb.SEARCH_VAR_DOMAIN | gdb.SEARCH_TYPE_DOMAIN)
```

The available address class categories in `gdb.Symbol` are represented
as constants in the `gdb` module:

`gdb.SYMBOL_LOC_UNDEF`

If this is returned by address class, it indicates an error either in
the symbol information or in GDB’s handling of symbols.

`gdb.SYMBOL_LOC_CONST`

Value is constant int.

`gdb.SYMBOL_LOC_STATIC`

Value is at a fixed address.

`gdb.SYMBOL_LOC_REGISTER`

Value is in a register.

`gdb.SYMBOL_LOC_ARG`

Value is an argument. This value is at the offset stored within the
symbol inside the frame’s argument list.

`gdb.SYMBOL_LOC_REF_ARG`

Value address is stored in the frame’s argument list. Just like
`LOC_ARG` except that the value’s address is stored at the
offset, not the value itself.

`gdb.SYMBOL_LOC_REGPARM_ADDR`

Value is a specified register. Just like `LOC_REGISTER` except
the register holds the address of the argument instead of the argument
itself.

`gdb.SYMBOL_LOC_LOCAL`

Value is a local variable.

`gdb.SYMBOL_LOC_TYPEDEF`

Value not used. Symbols in the domain `SYMBOL_STRUCT_DOMAIN` all
have this class.

`gdb.SYMBOL_LOC_LABEL`

Value is a label.

`gdb.SYMBOL_LOC_BLOCK`

Value is a block.

`gdb.SYMBOL_LOC_CONST_BYTES`

Value is a byte-sequence.

`gdb.SYMBOL_LOC_UNRESOLVED`

Value is at a fixed address, but the address of the variable has to be
determined from the minimal symbol table whenever the variable is
referenced.

`gdb.SYMBOL_LOC_OPTIMIZED_OUT`

The value does not actually exist in the program.

`gdb.SYMBOL_LOC_COMPUTED`

The value’s address is a computed location.

`gdb.SYMBOL_LOC_COMMON_BLOCK`

The value’s address is a symbol. This is only used for Fortran common
blocks.

## 23.3.2.31 Symbol table representation in Python

Access to symbol table data maintained by GDB on the inferior
is exposed to Python via two objects: `gdb.Symtab_and_line` and
`gdb.Symtab`. Symbol table and line data for a frame is returned
from the `find_sal` method in `gdb.Frame` object.
See [Frames In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frames-In-Python.html#Frames-In-Python).

For more information on GDB’s symbol table management, see
[Examining the Symbol Table](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbols.html#Symbols), for more information.

A `gdb.Symtab_and_line` object has the following attributes:

Variable: **Symtab\_and\_line.symtab**

The symbol table object (`gdb.Symtab`) for this frame.
This attribute is not writable.

Variable: **Symtab\_and\_line.pc**

Indicates the start of the address range occupied by code for the
current source line. This attribute is not writable.

Variable: **Symtab\_and\_line.last**

Indicates the end of the address range occupied by code for the current
source line. This attribute is not writable.

Variable: **Symtab\_and\_line.line**

Indicates the current line number for this object. This
attribute is not writable.

A `gdb.Symtab_and_line` object has the following methods:

Function: **Symtab\_and\_line.is\_valid** _()_

Returns `True` if the `gdb.Symtab_and_line` object is valid,
`False` if not. A `gdb.Symtab_and_line` object can become
invalid if the Symbol table and line object it refers to does not
exist in GDB any longer. All other
`gdb.Symtab_and_line` methods will throw an exception if it is
invalid at the time the method is called.

A `gdb.Symtab` object has the following attributes:

Variable: **Symtab.filename**

The symbol table’s source filename. This attribute is not writable.

Variable: **Symtab.objfile**

The symbol table’s backing object file. See [Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfiles-In-Python).
This attribute is not writable.

Variable: **Symtab.producer**

The name and possibly version number of the program that
compiled the code in the symbol table.
The contents of this string is up to the compiler.
If no producer information is available then `None` is returned.
This attribute is not writable.

A `gdb.Symtab` object has the following methods:

Function: **Symtab.is\_valid** _()_

Returns `True` if the `gdb.Symtab` object is valid,
`False` if not. A `gdb.Symtab` object can become invalid if
the symbol table it refers to does not exist in GDB any
longer. All other `gdb.Symtab` methods will throw an exception
if it is invalid at the time the method is called.

Function: **Symtab.fullname** _()_

Return the symbol table’s source absolute file name.

Function: **Symtab.global\_block** _()_

Return the global block of the underlying symbol table.
See [Blocks In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Blocks-In-Python.html#Blocks-In-Python).

Function: **Symtab.static\_block** _()_

Return the static block of the underlying symbol table.
See [Blocks In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Blocks-In-Python.html#Blocks-In-Python).

Function: **Symtab.linetable** _()_

Return the line table associated with the symbol table.
See [Line Tables In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Line-Tables-In-Python.html#Line-Tables-In-Python).

Function: **Symtab.source\_lines** _(first = `1`, last = `0`, unstyled = `False`)_

Read source lines from the file associated with this Symtab, this will
be the filename pointed to by `Symtab.fullname`. The source
lines are returned as a tuple of strings, each string represents a
single source line.

The first and last arguments can be used to only return
some lines from the file, first will be the first line returned,
and last will be the last line returned, as such first
must be less than, or equal to, last.

The default for first is `1`, which is the first line of
the file. The default for last is `0`, which is a special
value meaning the last line of the file.

When unstyled is `True` then the strings returned will have
no style information included. When unstyled is `False`,
the default, then the returned strings will include style information
if styling is enabled (see [Output Styling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Output-Styling.html#Output-Styling)), and GDB knows
how to style the source file. Styling information will take the form of
ANSI terminal escape sequences embedded within the strings.

If the source file cannot be read, for example, if the source file is
missing, then this method returns `None`.

## 23.3.2.32 Manipulating line tables using Python

Python code can request and inspect line table information from a
symbol table that is loaded in GDB. A line table is a
mapping of source lines to their executable locations in memory. To
acquire the line table information for a particular symbol table, use
the `linetable` function (see [Symbol Tables In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Symbol-Tables-In-Python.html#Symbol-Tables-In-Python)).

A `gdb.LineTable` is iterable. The iterator returns
`LineTableEntry` objects that correspond to the source line and
address for each line table entry. `LineTableEntry` objects have
the following attributes:

Variable: **LineTableEntry.line**

The source line number for this line table entry. This number
corresponds to the actual line of source. This attribute is not
writable.

Variable: **LineTableEntry.pc**

The address that is associated with the line table entry where the
executable code for that source line resides in memory. This
attribute is not writable.

As there can be multiple addresses for a single source line, you may
receive multiple `LineTableEntry` objects with matching
`line` attributes, but with different `pc` attributes. The
iterator is sorted in ascending `pc` order. Here is a small
example illustrating iterating over a line table.

```
symtab = gdb.selected_frame().find_sal().symtab
linetable = symtab.linetable()
for line in linetable:
   print ("Line: "+str(line.line)+" Address: "+hex(line.pc))
```

This will have the following output:

```
Line: 33 Address: 0x4005c8L
Line: 37 Address: 0x4005caL
Line: 39 Address: 0x4005d2L
Line: 40 Address: 0x4005f8L
Line: 42 Address: 0x4005ffL
Line: 44 Address: 0x400608L
Line: 42 Address: 0x40060cL
Line: 45 Address: 0x400615L
```

In addition to being able to iterate over a `LineTable`, it also
has the following direct access methods:

Function: **LineTable.line** _(line)_

Return a Python `Tuple` of `LineTableEntry` objects for any
entries in the line table for the given line, which specifies
the source code line. If there are no entries for that source code
line, the Python `None` is returned.

Function: **LineTable.has\_line** _(line)_

Return a Python `Boolean` indicating whether there is an entry in
the line table for this source line. Return `True` if an entry
is found, or `False` if not.

Function: **LineTable.source\_lines** _()_

Return a Python `List` of the source line numbers in the symbol
table. Only lines with executable code locations are returned. The
contents of the `List` will just be the source line entries
represented as Python `Long` values.

## 23.3.2.33 Manipulating breakpoints using Python

Python code can manipulate breakpoints via the `gdb.Breakpoint`
class.

A breakpoint can be created using one of the two forms of the
`gdb.Breakpoint` constructor. The first one accepts a string
like one would pass to the `break`
(see [Setting Breakpoints](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Set-Breaks.html#Set-Breaks)) and `watch`
(see [Setting Watchpoints](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Set-Watchpoints.html#Set-Watchpoints)) commands, and can be used to
create both breakpoints and watchpoints. The second accepts separate Python
arguments similar to [Explicit Locations](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Explicit-Locations.html#Explicit-Locations), and can only be used to create
breakpoints.

Function: **Breakpoint.\_\_init\_\_** _(spec \[, type \]\[, wp\_class \]\[, internal \]\[, temporary \]\[, qualified \])_

Create a new breakpoint according to spec, which is a string naming the
location of a breakpoint, or an expression that defines a watchpoint. The
string should describe a location in a format recognized by the `break`
command (see [Setting Breakpoints](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Set-Breaks.html#Set-Breaks)) or, in the case of a
watchpoint, by the `watch` command
(see [Setting Watchpoints](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Set-Watchpoints.html#Set-Watchpoints)).

The optional type argument specifies the type of the breakpoint to create,
as defined below.

The optional wp\_class argument defines the class of watchpoint to create,
if type is `gdb.BP_WATCHPOINT`. If wp\_class is omitted, it
defaults to `gdb.WP_WRITE`.

The optional internal argument allows the breakpoint to become invisible
to the user. The breakpoint will neither be reported when created, nor will it
be listed in the output from `info breakpoints` (but will be listed with
the `maint info breakpoints` command).

The optional temporary argument makes the breakpoint a temporary
breakpoint. Temporary breakpoints are deleted after they have been hit. Any
further access to the Python breakpoint after it has been hit will result in a
runtime error (as that breakpoint has now been automatically deleted).

The optional qualified argument is a boolean that allows interpreting
the function passed in `spec` as a fully-qualified name. It is equivalent
to `break`’s `-qualified` flag (see [Linespec Locations](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Linespec-Locations.html#Linespec-Locations) and
[Explicit Locations](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Explicit-Locations.html#Explicit-Locations)).

Function: **Breakpoint.\_\_init\_\_** _(\[ source \]\[, function \]\[, label \]\[, line \], \]\[ internal \]\[, temporary \]\[, qualified \])_

This second form of creating a new breakpoint specifies the explicit
location (see [Explicit Locations](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Explicit-Locations.html#Explicit-Locations)) using keywords. The new breakpoint will
be created in the specified source file source, at the specified
function, label and line.

internal, temporary and qualified have the same usage as
explained previously.

The available types are represented by constants defined in the `gdb`
module:

`gdb.BP_BREAKPOINT`

Normal code breakpoint.

`gdb.BP_HARDWARE_BREAKPOINT`

Hardware assisted code breakpoint.

`gdb.BP_WATCHPOINT`

Watchpoint breakpoint.

`gdb.BP_HARDWARE_WATCHPOINT`

Hardware assisted watchpoint.

`gdb.BP_READ_WATCHPOINT`

Hardware assisted read watchpoint.

`gdb.BP_ACCESS_WATCHPOINT`

Hardware assisted access watchpoint.

`gdb.BP_CATCHPOINT`

Catchpoint. Currently, this type can’t be used when creating
`gdb.Breakpoint` objects, but will be present in
`gdb.Breakpoint` objects reported from
`gdb.BreakpointEvent`s (see [Events In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Events-In-Python.html#Events-In-Python)).

The available watchpoint types are represented by constants defined in the
`gdb` module:

`gdb.WP_READ`

Read only watchpoint.

`gdb.WP_WRITE`

Write only watchpoint.

`gdb.WP_ACCESS`

Read/Write watchpoint.

Function: **Breakpoint.stop** _(self)_

The `gdb.Breakpoint` class can be sub-classed and, in
particular, you may choose to implement the `stop` method.
If this method is defined in a sub-class of `gdb.Breakpoint`,
it will be called when the inferior reaches any location of a
breakpoint which instantiates that sub-class. If the method returns
`True`, the inferior will be stopped at the location of the
breakpoint, otherwise the inferior will continue.

If there are multiple breakpoints at the same location with a
`stop` method, each one will be called regardless of the
return status of the previous. This ensures that all `stop`
methods have a chance to execute at that location. In this scenario
if one of the methods returns `True` but the others return
`False`, the inferior will still be stopped.

You should not alter the execution state of the inferior (i.e., step,
next, etc.), alter the current frame context (i.e., change the current
active frame), or alter, add or delete any breakpoint. As a general
rule, you should not alter any data within GDB or the inferior
at this time.

Example `stop` implementation:

```
class MyBreakpoint (gdb.Breakpoint):
      def stop (self):
        inf_val = gdb.parse_and_eval("foo")
        if inf_val == 3:
          return True
        return False
```

Function: **Breakpoint.is\_valid** _()_

Return `True` if this `Breakpoint` object is valid,
`False` otherwise. A `Breakpoint` object can become invalid
if the user deletes the breakpoint. In this case, the object still
exists, but the underlying breakpoint does not. In the cases of
watchpoint scope, the watchpoint remains valid even if execution of the
inferior leaves the scope of that watchpoint.

Function: **Breakpoint.delete** _()_

Permanently deletes the GDB breakpoint. This also
invalidates the Python `Breakpoint` object. Any further access
to this object’s attributes or methods will raise an error.

Variable: **Breakpoint.enabled**

This attribute is `True` if the breakpoint is enabled, and
`False` otherwise. This attribute is writable. You can use it to enable
or disable the breakpoint.

Variable: **Breakpoint.silent**

This attribute is `True` if the breakpoint is silent, and
`False` otherwise. This attribute is writable.

Note that a breakpoint can also be silent if it has commands and the
first command is `silent`. This is not reported by the
`silent` attribute.

Variable: **Breakpoint.pending**

This attribute is `True` if the breakpoint is pending, and
`False` otherwise. See [Set Breaks](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Set-Breaks.html#Set-Breaks). This attribute is
read-only.

Variable: **Breakpoint.thread**

If the breakpoint is thread-specific (see [Thread-Specific Breakpoints](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Thread_002dSpecific-Breakpoints.html#Thread_002dSpecific-Breakpoints)), this attribute holds the thread’s global id. If the
breakpoint is not thread-specific, this attribute is `None`.
This attribute is writable.

Only one of `Breakpoint.thread` or `Breakpoint.inferior` can
be set to a valid id at any time, that is, a breakpoint can be thread
specific, or inferior specific, but not both.

Variable: **Breakpoint.inferior**

If the breakpoint is inferior-specific (see [Inferior-Specific Breakpoints](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferior_002dSpecific-Breakpoints.html#Inferior_002dSpecific-Breakpoints)), this attribute holds the inferior’s id. If the
breakpoint is not inferior-specific, this attribute is `None`.

This attribute can be written for breakpoints of type
`gdb.BP_BREAKPOINT` and `gdb.BP_HARDWARE_BREAKPOINT`.

Variable: **Breakpoint.task**

If the breakpoint is Ada task-specific, this attribute holds the Ada task
id. If the breakpoint is not task-specific (or the underlying
language is not Ada), this attribute is `None`. This attribute
is writable.

Variable: **Breakpoint.ignore\_count**

This attribute holds the ignore count for the breakpoint, an integer.
This attribute is writable.

Variable: **Breakpoint.number**

This attribute holds the breakpoint’s number — the identifier used by
the user to manipulate the breakpoint. This attribute is not writable.

Variable: **Breakpoint.type**

This attribute holds the breakpoint’s type — the identifier used to
determine the actual breakpoint type or use-case. This attribute is not
writable.

Variable: **Breakpoint.visible**

This attribute tells whether the breakpoint is visible to the user
when set, or when the ‘ `info breakpoints`’ command is run. This
attribute is not writable.

Variable: **Breakpoint.temporary**

This attribute indicates whether the breakpoint was created as a
temporary breakpoint. Temporary breakpoints are automatically deleted
after that breakpoint has been hit. Access to this attribute, and all
other attributes and functions other than the `is_valid`
function, will result in an error after the breakpoint has been hit
(as it has been automatically deleted). This attribute is not
writable.

Variable: **Breakpoint.hit\_count**

This attribute holds the hit count for the breakpoint, an integer.
This attribute is writable, but currently it can only be set to zero.

Variable: **Breakpoint.location**

This attribute holds the location of the breakpoint, as specified by
the user. It is a string. If the breakpoint does not have a location
(that is, it is a watchpoint) the attribute’s value is `None`. This
attribute is not writable.

Variable: **Breakpoint.locations**

Get the most current list of breakpoint locations that are inserted for this
breakpoint, with elements of type `gdb.BreakpointLocation`
(described below). This functionality matches that of the
`info breakpoint` command (see [Set Breaks](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Set-Breaks.html#Set-Breaks)), in that it only retrieves
the most current list of locations, thus the list itself when returned is
not updated behind the scenes. This attribute is not writable.

Variable: **Breakpoint.expression**

This attribute holds a breakpoint expression, as specified by
the user. It is a string. If the breakpoint does not have an
expression (the breakpoint is not a watchpoint) the attribute’s value
is `None`. This attribute is not writable.

Variable: **Breakpoint.condition**

This attribute holds the condition of the breakpoint, as specified by
the user. It is a string. If there is no condition, this attribute’s
value is `None`. This attribute is writable.

Variable: **Breakpoint.commands**

This attribute holds the commands attached to the breakpoint. If
there are commands, this attribute’s value is a string holding all the
commands, separated by newlines. If there are no commands, this
attribute is `None`. This attribute is writable.

#### Breakpoint Locations

A breakpoint location is one of the actual places where a breakpoint has been
set, represented in the Python API by the `gdb.BreakpointLocation`
type. This type is never instantiated by the user directly, but is retrieved
from `Breakpoint.locations` which returns a list of breakpoint
locations where it is currently set. Breakpoint locations can become
invalid if new symbol files are loaded or dynamically loaded libraries are
closed. Accessing the attributes of an invalidated breakpoint location will
throw a `RuntimeError` exception. Access the `Breakpoint.locations`
attribute again to retrieve the new and valid breakpoints location list.

Variable: **BreakpointLocation.source**

This attribute returns the source file path and line number where this location
was set. The type of the attribute is a tuple of string and
long. If the breakpoint location doesn’t have a source location,
it returns None, which is the case for watchpoints and catchpoints.
This will throw a `RuntimeError` exception if the location
has been invalidated. This attribute is not writable.

Variable: **BreakpointLocation.address**

This attribute returns the address where this location was set.
This attribute is of type long. This will throw a `RuntimeError`
exception if the location has been invalidated. This attribute is
not writable.

Variable: **BreakpointLocation.enabled**

This attribute holds the value for whether or not this location is enabled.
This attribute is writable (boolean). This will throw a `RuntimeError`
exception if the location has been invalidated.

Variable: **BreakpointLocation.owner**

This attribute holds a reference to the `gdb.Breakpoint` owner object,
from which this `gdb.BreakpointLocation` was retrieved from.
This will throw a `RuntimeError` exception if the location has been
invalidated. This attribute is not writable.

Variable: **BreakpointLocation.function**

This attribute gets the name of the function where this location was set.
If no function could be found this attribute returns `None`.
This will throw a `RuntimeError` exception if the location has
been invalidated. This attribute is not writable.

Variable: **BreakpointLocation.fullname**

This attribute gets the full name of where this location was set. If no
full name could be found, this attribute returns `None`.
This will throw a `RuntimeError` exception if the location has
been invalidated. This attribute is not writable.

Variable: **BreakpointLocation.thread\_groups**

This attribute gets the thread groups it was set in. It returns a `List`
of the thread group ID’s. This will throw a `RuntimeError`
exception if the location has been invalidated. This attribute
is not writable.

## 23.3.2.34 Finish Breakpoints

A finish breakpoint is a temporary breakpoint set at the return address of
a frame, based on the `finish` command. `gdb.FinishBreakpoint`
extends `gdb.Breakpoint`. The underlying breakpoint will be disabled
and deleted when the execution will run out of the breakpoint scope (i.e.
`Breakpoint.stop` or `FinishBreakpoint.out_of_scope` triggered).
Finish breakpoints are thread specific and must be create with the right
thread selected.

It is not possible to create a `gdb.FinishBreakpoint` for an
inline frame (see [Inline Functions](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inline-Functions.html#Inline-Functions)).

Function: **FinishBreakpoint.\_\_init\_\_** _(\[frame\]\[, internal\])_

Create a finish breakpoint at the return address of the `gdb.Frame`
object frame. If frame is not provided, this defaults to the
newest frame. The optional internal argument allows the breakpoint to
become invisible to the user. See [Breakpoints In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Breakpoints-In-Python.html#Breakpoints-In-Python), for further
details about this argument.

Function: **FinishBreakpoint.out\_of\_scope** _(self)_

In some circumstances (e.g. `longjmp`, C `++` exceptions, GDB`return` command, …), a function may not properly terminate, and
thus never hit the finish breakpoint. When GDB notices such a
situation, the `out_of_scope` callback will be triggered.

You may want to sub-class `gdb.FinishBreakpoint` and override this
method:

```
class MyFinishBreakpoint (gdb.FinishBreakpoint)
    def stop (self):
        print ("normal finish")
        return True

    def out_of_scope ():
        print ("abnormal finish")
```

Variable: **FinishBreakpoint.return\_value**

When GDB is stopped at a finish breakpoint and the frame
used to build the `gdb.FinishBreakpoint` object had debug symbols, this
attribute will contain a `gdb.Value` object corresponding to the return
value of the function. The value will be `None` if the function return
type is `void` or if the return value was not computable. This attribute
is not writable.

## 23.3.2.35 Python representation of lazy strings

A _lazy string_ is a string whose contents is not retrieved or
encoded until it is needed.

A `gdb.LazyString` is represented in GDB as an
`address` that points to a region of memory, an `encoding`
that will be used to encode that region of memory, and a `length`
to delimit the region of memory that represents the string. The
difference between a `gdb.LazyString` and a string wrapped within
a `gdb.Value` is that a `gdb.LazyString` will only be
retrieved and encoded during printing, while a `gdb.Value`
wrapping a string is immediately retrieved and encoded on creation.

A `gdb.LazyString` can be created using the
`gdb.Value.lazy_string` method (see [Values From Inferior](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Values-From-Inferior.html#Values-From-Inferior)).

A `gdb.LazyString` object has the following functions:

Function: **LazyString.value** _()_

Convert the `gdb.LazyString` to a `gdb.Value`. This value
will point to the string in memory, but will lose all the delayed
retrieval, encoding and handling that GDB applies to a
`gdb.LazyString`.

Variable: **LazyString.address**

This attribute holds the address of the string. This attribute is not
writable.

Variable: **LazyString.length**

This attribute holds the length of the string in characters. If the
length is -1, then the string will be fetched and encoded up to the
first null of appropriate width. This attribute is not writable.

Variable: **LazyString.encoding**

This attribute holds the encoding that will be applied to the string
when the string is printed by GDB. If the encoding is not
set, or contains an empty string, then GDB will select the
most appropriate encoding when the string is printed. This attribute
is not writable.

Variable: **LazyString.type**

This attribute holds the type that is represented by the lazy string’s
type. For a lazy string this is a pointer or array type. To
resolve this to the lazy string’s character type, use the type’s
`target` method. See [Types In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Types-In-Python.html#Types-In-Python). This attribute is not
writable.

## 23.3.2.36 Python representation of colors

You can assign instance of `gdb.Color` to the `value` of
a `Parameter` instance created with `PARAM_COLOR`.

`gdb.Color` may refer to an index from a color palette or contain
components of a color from some color space.

Function: **Color.\_\_init\_\_** _(\[value \[, color\_space\]\])_

value is `None` (meaning the terminal’s default color),
an integer index of a color in palette, tuple with color components
or a string. String can be a hex RGB triplet in ‘ `#RRGGBB`’ format
or one of the following color names:
‘ `none`’ (meaning the terminal’s default color), ‘ `black`’, ‘ `red`’,
‘ `green`’, ‘ `yellow`’, ‘ `blue`’, ‘ `magenta`’, ‘ `cyan`’,
or ‘ `white`’.

color\_space should be one of the ‘ `COLORSPACE_`’ constants
listed below. This argument tells GDB which color space
value belongs.

Variable: **Color.is\_none**

This attribute is boolean. If its value is `True` then color is terminal’s
default.

Variable: **Color.is\_indexed**

This attribute is boolean. If its value is `True` then color is indexed,
i.e. belongs to some palette.

Variable: **Color.is\_direct**

This attribute is boolean. If its value is `True` then this object
describes direct colour in the sense of ISO/IEC 8613-6.

Variable: **Color.index**

This attribute exist if `is_indexed` is `True`. Its integer value is
index of a color in a palette.

Variable: **Color.components**

This attribute exist if `is_direct` is `True`. Its value is tuple
with integer components of a color.

Function: **Color.escape\_sequence** _(is\_foreground)_

Returns string to change terminal’s color to this.

If is\_foreground is `True`, then the returned sequence will change
foreground color. Otherwise, the returned sequence will change background
color.

If styling is currently disabled (see [`set style\\
enabled`](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Output-Styling.html#Output-Styling)), then this method will return an empty string.

When color is initialized, its color space must be specified. The
available color spaces are represented by constants defined in the `gdb`
module:

`gdb.COLORSPACE_MONOCHROME`

Palette with only terminal’s default color.

`gdb.COLORSPACE_ANSI_8COLOR`

Palette with eight standard colors of ISO/IEC 6429 "black", "red", "green", etc.

`gdb.COLORSPACE_AIXTERM_16COLOR`

Palette with 16 colors. First eight are standard colors of ISO/IEC 6429
"black", "red", "green", etc. Next eight are their bright version.

`gdb.COLORSPACE_XTERM_256COLOR`

Palette with 256 colors. First 16 are from COLORSPACE\_AIXTERM\_16COLOR. Next
216 colors are 6x6x6 RGB cube. And last 24 colors form grayscale ramp.

`gdb.COLORSPACE_RGB_24BIT`

Direct 24-bit RGB colors.

It is not possible to sub-class the `Color` class.

## 23.3.2.37 Python representation of styles

A style object contains the foreground and background colors
(see [Colors In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Colors-In-Python.html#Colors-In-Python)), along with an intensity, and can be used
to apply this styling to output produced from Python.

GDB has many styles builtin (see [Output Styling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Output-Styling.html#Output-Styling)), and
style objects can be created that apply these builtin styles to Python
output. It is also possible to create new styles which can be used to
style Python output (see [Creating Style Parameters](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Parameters-In-Python.html#Creating-Style-Parameters)).

The style class is called `gdb.Style`, and has the following
methods and attributes:

Function: **Style.\_\_init\_\_** _(style\_name)_

Create a `gdb.Style` that represents a builtin named style. The
style\_name must be a non-empty string that names a style that
exists as a setting in GDB within ‘ `set style …`’. For
example, the string ‘ `"filename"`’ can be used to create a
`gdb.Style` object representing the ‘ `set style filename`’
style.

If style\_name names an unknown style then a `RuntimeError`
exception is raised.

Function: **Style.\_\_init\_\_** _(foreground=`None`, background=`None`, intensity=gdb.INTENSITY\_NORMAL)_

Create a custom `gdb.Style`, manually specifying the three
individual components. All of the arguments are optional. By
default, if no arguments are given, then the default style will be
created, this will produce output with the default terminal foreground
and background colors, along with the normal level of intensity
(i.e. neither bold, nor dim).

The foreground and background arguments should either be
`None`, in which case the terminal default colors are used, or a
`gdb.Color` object (see [Colors In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Colors-In-Python.html#Colors-In-Python)). Any other object
type will result in a `TypeError` exception being raised.

The intensity argument should be one of the intensity constants
defined below (see [Style Intensities](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Styles-In-Python.html#Style-Intensities)). Passing a none integer
value results in a `TypeError` exception, and passing an invalid
constant results in a `ValueError` exception.

Function: **Style.escape\_sequence** _()_

If styling is enabled (see [Output Styling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Output-Styling.html#Output-Styling), then this method
returns the string which is the terminal escape sequence necessary to
apply this `gdb.Style`.

If styling is disabled then this method returns the empty string.

If this is a named style, which for some reason can no longer be read,
then a `RuntimeError` exception is raised.

Function: **Style.apply** _(string)_

This method returns string, which must be a `str` object,
with an escape sequence at both the start, and at the end. The escape
sequence at the start applies this style, while the escape sequence at
the end applies the terminal’s default style.

The effect of this is that, printing the result of this method, will
print string with this style applied. Future output will be
printed with the terminal’s default style.

If styling is currently disabled (see [Output Styling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Output-Styling.html#Output-Styling)), then
`Style.apply` just returns a copy of string with no escape
sequences added.

If this is a named style, which for some reason can no longer be read,
then a `RuntimeError` exception is raised.

Variable: **Style.foreground**

This read/write attribute contains the `gdb.Color` object
representing this style’s foreground color. Writing to this attribute
updates the style’s foreground color.

If the `gdb.Style` object was created using a named style (i.e.
using the single argument style\_name`__init__` method),
then the underlying setting will be updated.

For unnamed styles, only the `gdb.Style` object will change.

Variable: **Style.background**

This read/write attribute contains the `gdb.Color` object
representing this style’s background color. Writing to this attribute
updates the style’s background color.

If the `gdb.Style` object was created using a named style (i.e.
using the single argument style\_name`__init__` method),
then the underlying setting will be updated.

For unnamed styles, only the `gdb.Style` object will change.

Variable: **Style.intensity**

This read/write attribute contains the intensity for this style, and
will be one of the constants defined below (see [Style Intensities](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Styles-In-Python.html#Style-Intensities)). Writing to this attribute updates the style’s
intensity.

It is also possible to write `None` to this attribute, this is
equivalent of writing `gdb.INTENSITY_NORMAL`. This attribute
will never read as `None`.

If the `gdb.Style` object was created using a named style (i.e.
using the single argument style\_name`__init__` method),
then the underlying setting will be updated.

For unnamed styles, only the `gdb.Style` object will change.

The following constants are defined to represent the different style
intensities:

`gdb.INTENSITY_NORMAL`

This is the terminal’s default intensity.

`gdb.INTENSITY_BOLD`

This is for bold text.

`gdb.INTENSITY_DIM`

This is for dim text.

## 23.3.2.38 Python representation of architectures

GDB uses architecture specific parameters and artifacts in a
number of its various computations. An architecture is represented
by an instance of the `gdb.Architecture` class.

A `gdb.Architecture` class has the following methods:

Function: **Architecture.name** _()_

Return the name (string value) of the architecture.

Function: **Architecture.disassemble** _(start\_pc \[, end\_pc \[, count\]\]\[, styling = `False`\])_

Return a list of disassembled instructions starting from the memory
address start\_pc. The optional arguments end\_pc and
count determine the number of instructions in the returned list.
If both the optional arguments end\_pc and count are
specified, then a list of at most count disassembled instructions
whose start address falls in the closed memory address interval from
start\_pc to end\_pc are returned. If end\_pc is not
specified, but count is specified, then count number of
instructions starting from the address start\_pc are returned. If
count is not specified but end\_pc is specified, then all
instructions whose start address falls in the closed memory address
interval from start\_pc to end\_pc are returned. If neither
end\_pc nor count are specified, then a single instruction at
start\_pc is returned. For all of these cases, each element of the
returned list is a Python `dict` with the following string keys:

`addr`

The value corresponding to this key is a Python long integer capturing
the memory address of the instruction.

`asm`

The value corresponding to this key is a string value which represents
the instruction with assembly language mnemonics. The assembly
language flavor used is the same as that specified by the current CLI
variable `disassembly-flavor`. See [Machine Code](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Machine-Code.html#Machine-Code).

When the optional argument styling is `True` the ‘ `asm`’
string can contain ANSI terminal escape sequences if styling is
enabled (see [Output Styling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Output-Styling.html#Output-Styling)). When styling is `False`,
which is the default, then no styling will be applied to ‘ `asm`’.

`length`

The value corresponding to this key is the length (integer value) of the
instruction in bytes.

Function: **Architecture.integer\_type** _(size \[, signed\])_

This function looks up an integer type by its size, and
optionally whether or not it is signed.

size is the size, in bits, of the desired integer type. Only
certain sizes are currently supported: 0, 8, 16, 24, 32, 64, and 128.

If signed is not specified, it defaults to `True`. If
signed is `False`, the returned type will be unsigned.

If the indicated type cannot be found, this function will throw a
`ValueError` exception.

Function: **Architecture.void\_type** _()_

This function returns a void type.

Function: **Architecture.registers** _(\[ reggroup \])_

Return a `gdb.RegisterDescriptorIterator` (see [Registers In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Registers-In-Python.html#Registers-In-Python)) for all of the registers in reggroup, a string that is
the name of a register group. If reggroup is omitted, or is the
empty string, then the register group ‘ `all`’ is assumed.

Function: **Architecture.register\_groups** _()_

Return a `gdb.RegisterGroupsIterator` (see [Registers In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Registers-In-Python.html#Registers-In-Python)) for all of the register groups available for the
`gdb.Architecture`.

## 23.3.2.39 Registers In Python

Python code can request from a `gdb.Architecture` information
about the set of registers available
(see [`Architecture.registers`](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Architectures-In-Python.html#gdbpy_005farchitecture_005fregisters)).
The register information is returned as a
`gdb.RegisterDescriptorIterator`, which is an iterator that in
turn returns `gdb.RegisterDescriptor` objects.

A `gdb.RegisterDescriptor` does not provide the value of a
register (see [`Frame.read_register`](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Frames-In-Python.html#gdbpy_005fframe_005fread_005fregister)
for reading a register’s value), instead the `RegisterDescriptor`
is a way to discover which registers are available for a particular
architecture.

A `gdb.RegisterDescriptor` has the following read-only properties:

Variable: **RegisterDescriptor.name**

The name of this register.

It is also possible to lookup a register descriptor based on its name
using the following `gdb.RegisterDescriptorIterator` function:

Function: **RegisterDescriptorIterator.find** _(name)_

Takes name as an argument, which must be a string, and returns a
`gdb.RegisterDescriptor` for the register with that name, or
`None` if there is no register with that name.

Python code can also request from a `gdb.Architecture`
information about the set of register groups available on a given
architecture
(see [`Architecture.register_groups`](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Architectures-In-Python.html#gdbpy_005farchitecture_005freggroups)).

Every register can be a member of zero or more register groups. Some
register groups are used internally within GDB to control
things like which registers must be saved when calling into the
program being debugged (see [Calling Program Functions](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Calling.html#Calling)).
Other register groups exist to allow users to easily see related sets
of registers in commands like `info registers`
(see [`info registers\\
reggroup`](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Registers.html#info_005fregisters_005freggroup)).

The register groups information is returned as a
`gdb.RegisterGroupsIterator`, which is an iterator that in turn
returns `gdb.RegisterGroup` objects.

A `gdb.RegisterGroup` object has the following read-only
properties:

Variable: **RegisterGroup.name**

A string that is the name of this register group.

## 23.3.2.40 Connections In Python

GDB lets you run and debug multiple programs in a single
session. Each program being debugged has a connection, the connection
describes how GDB controls the program being debugged.
Examples of different connection types are ‘ `native`’ and
‘ `remote`’. See [Inferiors Connections and Programs](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-Connections-and-Programs.html#Inferiors-Connections-and-Programs).

Connections in GDB are represented as instances of
`gdb.TargetConnection`, or as one of its sub-classes. To get a
list of all connections use `gdb.connections`
(see [gdb.connections](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Basic-Python.html#gdbpy_005fconnections)).

To get the connection for a single `gdb.Inferior` read its
`gdb.Inferior.connection` attribute
(see [gdb.Inferior.connection](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#gdbpy_005finferior_005fconnection)).

Currently there is only a single sub-class of
`gdb.TargetConnection`, `gdb.RemoteTargetConnection`,
however, additional sub-classes may be added in future releases of
GDB. As a result you should avoid writing code like:

```
conn = gdb.selected_inferior().connection
if type(conn) is gdb.RemoteTargetConnection:
  print("This is a remote target connection")
```

as this may fail when more connection types are added. Instead, you
should write:

```
conn = gdb.selected_inferior().connection
if isinstance(conn, gdb.RemoteTargetConnection):
  print("This is a remote target connection")
```

A `gdb.TargetConnection` has the following method:

Function: **TargetConnection.is\_valid** _()_

Return `True` if the `gdb.TargetConnection` object is valid,
`False` if not. A `gdb.TargetConnection` will become
invalid if the connection no longer exists within GDB, this
might happen when no inferiors are using the connection, but could be
delayed until the user replaces the current target.

Reading any of the `gdb.TargetConnection` properties will throw
an exception if the connection is invalid.

A `gdb.TargetConnection` has the following read-only properties:

Variable: **TargetConnection.num**

An integer assigned by GDB to uniquely identify this
connection. This is the same value as displayed in the ‘ `Num`’
column of the `info connections` command output (see [info connections](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-Connections-and-Programs.html#Inferiors-Connections-and-Programs)).

Variable: **TargetConnection.type**

A string that describes what type of connection this is. This string
will be one of the valid names that can be passed to the `target`
command (see [target command](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Target-Commands.html#Target-Commands)).

Variable: **TargetConnection.description**

A string that gives a short description of this target type. This is
the same string that is displayed in the ‘ `Description`’ column of
the `info connection` command output (see [info connections](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-Connections-and-Programs.html#Inferiors-Connections-and-Programs)).

Variable: **TargetConnection.details**

An optional string that gives additional information about this
connection. This attribute can be `None` if there are no
additional details for this connection.

An example of a connection type that might have additional details is
the ‘ `remote`’ connection, in this case the details string can
contain the ‘ `hostname:port`’ that was used to connect
to the remote target.

The `gdb.RemoteTargetConnection` class is a sub-class of
`gdb.TargetConnection`, and is used to represent ‘ `remote`’
and ‘ `extended-remote`’ connections. In addition to the attributes
and methods available from the `gdb.TargetConnection` base class,
a `gdb.RemoteTargetConnection` has the following method:

Function: **RemoteTargetConnection.send\_packet** _(packet)_

This method sends packet to the remote target and returns the
response. The packet should either be a `bytes` object, or
a `Unicode` string.

If packet is a `Unicode` string, then the string is encoded
to a `bytes` object using the ASCII codec. If the string
can’t be encoded then an `UnicodeError` is raised.

If packet is not a `bytes` object, or a `Unicode`
string, then a `TypeError` is raised. If packet is empty
then a `ValueError` is raised.

The response is returned as a `bytes` object. If it is known
that the response can be represented as a string then this can be
decoded from the buffer. For example, if it is known that the
response is an ASCII string:

```
remote_connection.send_packet("some_packet").decode("ascii")
```

The prefix, suffix, and checksum (as required by the remote serial
protocol) are automatically added to the outgoing packet, and removed
from the incoming packet before the contents of the reply are
returned.

This is equivalent to the `maintenance packet` command
(see [maint packet](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Maintenance-Commands.html#maint-packet)).

## 23.3.2.41 Implementing new TUI windows

New TUI (see [TUI](https://sourceware.org/gdb/current/onlinedocs/gdb.html/TUI.html#TUI)) windows can be implemented in Python.

Function: **gdb.register\_window\_type** _(name, factory)_

Because TUI windows are created and destroyed depending on the layout
the user chooses, new window types are implemented by registering a
factory function with GDB.

name is the name of the new window. It’s an error to try to
replace one of the built-in windows, but other window types can be
replaced. The name should match the regular expression
`[a-zA-Z][-_.a-zA-Z0-9]*`, it is an error to try and create a
window with an invalid name.

function is a factory function that is called to create the TUI
window. This is called with a single argument of type
`gdb.TuiWindow`, described below. It should return an object
that implements the TUI window protocol, also described below.

As mentioned above, when a factory function is called, it is passed
an object of type `gdb.TuiWindow`. This object has these
methods and attributes:

Function: **TuiWindow.is\_valid** _()_

This method returns `True` when this window is valid. When the
user changes the TUI layout, windows no longer visible in the new
layout will be destroyed. At this point, the `gdb.TuiWindow`
will no longer be valid, and methods (and attributes) other than
`is_valid` will throw an exception.

When the TUI is disabled using `tui disable` (see [tui disable](https://sourceware.org/gdb/current/onlinedocs/gdb.html/TUI-Commands.html#TUI-Commands)) the window is hidden rather than destroyed,
but `is_valid` will still return `False` and other methods
(and attributes) will still throw an exception.

Variable: **TuiWindow.width**

This attribute holds the width of the window. It is not writable.

Variable: **TuiWindow.height**

This attribute holds the height of the window. It is not writable.

Variable: **TuiWindow.title**

This attribute holds the window’s title, a string. This is normally
displayed above the window. This attribute can be modified.

Function: **TuiWindow.erase** _()_

Remove all the contents of the window.

Function: **TuiWindow.write** _(string \[, full\_window\])_

Write string to the window. string can contain ANSI
terminal escape styling sequences; GDB will translate these
as appropriate for the terminal.

If the full\_window parameter is `True`, then string
contains the full contents of the window. This is similar to calling
`erase` before `write`, but avoids the flickering.

The factory function that you supply should return an object
conforming to the TUI window protocol. These are the methods that can
be called on this object, which is referred to below as the “window
object”. The methods documented below are optional; if the object
does not implement one of these methods, GDB will not attempt
to call it. Additional new methods may be added to the window
protocol in the future. GDB guarantees that they will begin
with a lower-case letter, so you can start implementation methods with
upper-case letters or underscore to avoid any future conflicts.

Function: **Window.close** _()_

When the TUI window is closed, the `gdb.TuiWindow` object will be
put into an invalid state. At this time, GDB will call
`close` method on the window object.

After this method is called, GDB will discard any references
it holds on this window object, and will no longer call methods on
this object.

Function: **Window.render** _()_

In some situations, a TUI window can change size. For example, this
can happen if the user resizes the terminal, or changes the layout.
When this happens, GDB will call the `render` method on
the window object.

If your window is intended to update in response to changes in the
inferior, you will probably also want to register event listeners and
send output to the `gdb.TuiWindow`.

Function: **Window.hscroll** _(num)_

This is a request to scroll the window horizontally. num is the
amount by which to scroll, with negative numbers meaning to scroll
right. In the TUI model, it is the viewport that moves, not the
contents. A positive argument should cause the viewport to move
right, and so the content should appear to move to the left.

Function: **Window.vscroll** _(num)_

This is a request to scroll the window vertically. num is the
amount by which to scroll, with negative numbers meaning to scroll
backward. In the TUI model, it is the viewport that moves, not the
contents. A positive argument should cause the viewport to move down,
and so the content should appear to move up.

Function: **Window.click** _(x, y, button)_

This is called on a mouse click in this window. x and y are
the mouse coordinates inside the window (0-based, from the top left
corner), and button specifies which mouse button was used, whose
values can be 1 (left), 2 (middle), or 3 (right).

When TUI mouse events are disabled by turning off the `tui mouse-events`
setting (see [set tui mouse-events](https://sourceware.org/gdb/current/onlinedocs/gdb.html/TUI-Configuration.html#tui_002dmouse_002devents)), then `click` will
not be called.

## 23.3.2.42 Instruction Disassembly In Python

GDB’s builtin disassembler can be extended, or even replaced,
using the Python API. The disassembler related features are contained
within the `gdb.disassembler` module:

class: **gdb.disassembler.DisassembleInfo**

Disassembly is driven by instances of this class. Each time
GDB needs to disassemble an instruction, an instance of this
class is created and passed to a registered disassembler. The
disassembler is then responsible for disassembling an instruction and
returning a result.

Instances of this type are usually created within GDB,
however, it is possible to create a copy of an instance of this type,
see the description of `__init__` for more details.

This class has the following properties and methods:

Variable: **DisassembleInfo.address**

A read-only integer containing the address at which GDB
wishes to disassemble a single instruction.

Variable: **DisassembleInfo.architecture**

The `gdb.Architecture` (see [Architectures In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Architectures-In-Python.html#Architectures-In-Python)) for
which GDB is currently disassembling, this property is
read-only.

Variable: **DisassembleInfo.progspace**

The `gdb.Progspace` (see [Program Spaces\\
In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python)) for which GDB is currently disassembling, this
property is read-only.

Function: **DisassembleInfo.is\_valid** _()_

Returns `True` if the `DisassembleInfo` object is valid,
`False` if not. A `DisassembleInfo` object will become
invalid once the disassembly call for which the `DisassembleInfo`
was created, has returned. Calling other `DisassembleInfo`
methods, or accessing `DisassembleInfo` properties, will raise a
`RuntimeError` exception if it is invalid.

Function: **DisassembleInfo.\_\_init\_\_** _(info)_

This can be used to create a new `DisassembleInfo` object that is
a copy of info. The copy will have the same `address`,
`architecture`, and `progspace` values as info, and
will become invalid at the same time as info.

This method exists so that sub-classes of `DisassembleInfo` can
be created, these sub-classes must be initialized as copies of an
existing `DisassembleInfo` object, but sub-classes might choose
to override the `read_memory` method, and so control what
GDB sees when reading from memory
(see [builtin\_disassemble](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#builtin_005fdisassemble)).

Function: **DisassembleInfo.read\_memory** _(length, offset)_

This method allows the disassembler to read the bytes of the
instruction to be disassembled. The method reads length bytes,
starting at offset from
`DisassembleInfo.address`.

It is important that the disassembler read the instruction bytes using
this method, rather than reading inferior memory directly, as in some
cases GDB disassembles from an internal buffer rather than
directly from inferior memory, calling this method handles this
detail.

Returns a buffer object, which behaves much like an array or a string,
just as `Inferior.read_memory` does
(see [Inferior.read\_memory](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#gdbpy_005finferior_005fread_005fmemory)). The
length of the returned buffer will always be exactly length.

If GDB is unable to read the required memory then a
`gdb.MemoryError` exception is raised (see [Exception Handling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Exception-Handling.html#Exception-Handling)).

This method can be overridden by a sub-class in order to control what
GDB sees when reading from memory
(see [builtin\_disassemble](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#builtin_005fdisassemble)). When overriding this method it is
important to understand how `builtin_disassemble` makes use of
this method.

While disassembling a single instruction there could be multiple calls
to this method, and the same bytes might be read multiple times. Any
single call might only read a subset of the total instruction bytes.

If an implementation of `read_memory` is unable to read the
requested memory contents, for example, if there’s a request to read
from an invalid memory address, then a `gdb.MemoryError` should
be raised.

Raising a `MemoryError` inside `read_memory` does not
automatically mean a `MemoryError` will be raised by
`builtin_disassemble`. It is possible the GDB’s builtin
disassembler is probing to see how many bytes are available. When
`read_memory` raises the `MemoryError` the builtin
disassembler might be able to perform a complete disassembly with the
bytes it has available, in this case `builtin_disassemble` will
not itself raise a `MemoryError`.

Any other exception type raised in `read_memory` will propagate
back and be re-raised by `builtin_disassemble`.

Function: **DisassembleInfo.text\_part** _(style, string)_

Create a new `DisassemblerTextPart` representing a piece of a
disassembled instruction. string should be a non-empty string,
and style should be an appropriate style constant
(see [Disassembler Style Constants](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#Disassembler-Style-Constants)).

Disassembler parts are used when creating a `DisassemblerResult`
in order to represent the styling within an instruction
(see [DisassemblerResult Class](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#DisassemblerResult-Class)).

Function: **DisassembleInfo.address\_part** _(address)_

Create a new `DisassemblerAddressPart`. address is the
value of the absolute address this part represents. A
`DisassemblerAddressPart` is displayed as an absolute address and
an associated symbol, the address and symbol are styled appropriately.

class: **gdb.disassembler.Disassembler**

This is a base class from which all user implemented disassemblers
must inherit.

Function: **Disassembler.\_\_init\_\_** _(name)_

The constructor takes name, a string, which should be a short
name for this disassembler.

Function: **Disassembler.\_\_call\_\_** _(info)_

The `__call__` method must be overridden by sub-classes to
perform disassembly. Calling `__call__` on this base class will
raise a `NotImplementedError` exception.

The info argument is an instance of `DisassembleInfo`, and
describes the instruction that GDB wants disassembling.

If this function returns `None`, this indicates to GDB
that this sub-class doesn’t wish to disassemble the requested
instruction. GDB will then use its builtin disassembler to
perform the disassembly.

Alternatively, this function can return a `DisassemblerResult`
that represents the disassembled instruction, this type is described
in more detail below.

The `__call__` method can raise a `gdb.MemoryError`
exception (see [Exception Handling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Exception-Handling.html#Exception-Handling)) to indicate to GDB
that there was a problem accessing the required memory, this will then
be displayed by GDB within the disassembler output.

Ideally, the only three outcomes from invoking `__call__` would
be a return of `None`, a successful disassembly returned in a
`DisassemblerResult`, or a `MemoryError` indicating that
there was a problem reading memory.

However, as an implementation of `__call__` could fail due to
other reasons, e.g. some external resource required to perform
disassembly is temporarily unavailable, then, if `__call__`
raises a `GdbError`, the exception will be converted to a string
and printed at the end of the disassembly output, the disassembly
request will then stop.

Any other exception type raised by the `__call__` method is
considered an error in the user code, the exception will be printed to
the error stream according to the `set python print-stack` setting
(see [`set python print-stack`](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Python-Commands.html#set_005fpython_005fprint_005fstack)).

class: **gdb.disassembler.DisassemblerResult**

This class represents the result of disassembling a single
instruction. An instance of this class will be returned from
`builtin_disassemble` (see [builtin\_disassemble](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#builtin_005fdisassemble)), and an
instance of this class should be returned from
`Disassembler.__call__` (see [Disassembler Class](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#Disassembler-Class)) if an
instruction was successfully disassembled.

It is not possible to sub-class the `DisassemblerResult` class.

The `DisassemblerResult` class has the following properties and
methods:

Function: **DisassemblerResult.\_\_init\_\_** _(length, string, parts)_

Initialize an instance of this class, length is the length of
the disassembled instruction in bytes, which must be greater than
zero.

Only one of string or parts should be used to initialize a
new `DisassemblerResult`; the other one should be passed the
value `None`. Alternatively, the arguments can be passed by
name, and the unused argument can be ignored.

The string argument, if not `None`, is a non-empty string
that represents the entire disassembled instruction. Building a result
object using the string argument does not allow for any styling
information to be included in the result. GDB will style the
result as a single `DisassemblerTextPart` with `STYLE_TEXT`
style (see [Disassembler Styling Parts](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#Disassembler-Styling-Parts)).

The parts argument, if not `None`, is a non-empty sequence
of `DisassemblerPart` objects. Each part represents a small part
of the disassembled instruction along with associated styling
information. A result object built using parts can be displayed
by GDB with full styling information
(see [`set style disassembler\\
enabled`](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Output-Styling.html#style_005fdisassembler_005fenabled)).

Variable: **DisassemblerResult.length**

A read-only property containing the length of the disassembled
instruction in bytes, this will always be greater than zero.

Variable: **DisassemblerResult.string**

A read-only property containing a non-empty string representing the
disassembled instruction. The string is a representation of the
disassembled instruction without any styling information. To see how
the instruction will be styled use the parts property.

If this instance was initialized using separate
`DisassemblerPart` objects, the string property will still
be valid. The string value is created by concatenating the
`DisassemblerPart.string` values of each component part
(see [Disassembler Styling Parts](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#Disassembler-Styling-Parts)).

Variable: **DisassemblerResult.parts**

A read-only property containing a non-empty sequence of
`DisassemblerPart` objects. Each `DisassemblerPart` object
contains a small part of the instruction along with information about
how that part should be styled. GDB uses this information to
create styled disassembler output
(see [`set style disassembler\\
enabled`](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Output-Styling.html#style_005fdisassembler_005fenabled)).

If this instance was initialized using a single string rather than
with a sequence of `DisassemblerPart` objects, the parts
property will still be valid. In this case the parts property
will hold a sequence containing a single `DisassemblerTextPart`
object, the string of which will represent the entire instruction, and
the style of which will be `STYLE_TEXT`.

class: **gdb.disassembler.DisassemblerPart**

This is a parent class from which the different part sub-classes
inherit. Only instances of the sub-classes detailed below will be
returned by the Python API.

It is not possible to directly create instances of either this parent
class, or any of the sub-classes listed below. Instances of the
sub-classes listed below are created by calling
`builtin_disassemble` (see [builtin\_disassemble](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#builtin_005fdisassemble)) and are
returned within the `DisassemblerResult` object, or can be
created by calling the `text_part` and `address_part`
methods on the `DisassembleInfo` class (see [DisassembleInfo Class](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#DisassembleInfo-Class)).

The `DisassemblerPart` class has a single property:

Variable: **DisassemblerPart.string**

A read-only property that contains a non-empty string representing
this part of the disassembled instruction. The string within this
property doesn’t include any styling information.

class: **gdb.disassembler.DisassemblerTextPart**

The `DisassemblerTextPart` class represents a piece of the
disassembled instruction and the associated style for that piece.
Instances of this class can’t be created directly, instead call
`DisassembleInfo.text_part` to create a new instance of this
class (see [DisassembleInfo Class](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#DisassembleInfo-Class)).

As well as the properties of its parent class, the
`DisassemblerTextPart` has the following additional property:

Variable: **DisassemblerTextPart.style**

A read-only property that contains one of the defined style constants.
GDB will use this style when styling this part of the
disassembled instruction (see [Disassembler Style Constants](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#Disassembler-Style-Constants)).

class: **gdb.disassembler.DisassemblerAddressPart**

The `DisassemblerAddressPart` class represents an absolute
address within a disassembled instruction. Using a
`DisassemblerAddressPart` instead of a
`DisassemblerTextPart` with `STYLE_ADDRESS` is preferred,
GDB will display the address as both an absolute address, and
will look up a suitable symbol to display next to the address. Using
`DisassemblerAddressPart` also ensures that user settings such as
`set print max-symbolic-offset` are respected.

Here is an example of an x86-64 instruction:

```
call   0x401136 <foo>
```

In this instruction the `0x401136 <foo>` was generated from a
single `DisassemblerAddressPart`. The `0x401136` will be
styled with `STYLE_ADDRESS`, and `foo` will be styled with
`STYLE_SYMBOL`. The `<` and `>` will be styled as
`STYLE_TEXT`.

If the inclusion of the symbol name is not required then a
`DisassemblerTextPart` with style `STYLE_ADDRESS` can be
used instead.

Instances of this class can’t be created directly, instead call
`DisassembleInfo.address_part` to create a new instance of this
class (see [DisassembleInfo Class](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#DisassembleInfo-Class)).

As well as the properties of its parent class, the
`DisassemblerAddressPart` has the following additional property:

Variable: **DisassemblerAddressPart.address**

A read-only property that contains the address passed to this
object’s `__init__` method.

The following table lists all of the disassembler styles that are
available. GDB maps these style constants onto its style
settings (see [Output Styling](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Output-Styling.html#Output-Styling)). In some cases, several style
constants produce the same style settings, and thus will produce the
same visual effect on the screen. This could change in future
releases of GDB, so care should be taken to select the
correct style constant to ensure correct output styling in future
releases of GDB.

`gdb.disassembler.STYLE_TEXT`

This is the default style used by GDB when styling
disassembler output. This style should be used for any parts of the
instruction that don’t fit any of the other styles listed below.
GDB styles text with this style using its default style.

`gdb.disassembler.STYLE_MNEMONIC`

This style is used for styling the primary instruction mnemonic, which
usually appears at, or near, the start of the disassembled instruction
string.

GDB styles text with this style using the `disassembler
mnemonic` style setting.

`gdb.disassembler.STYLE_SUB_MNEMONIC`

This style is used for styling any sub-mnemonics within a disassembled
instruction. A sub-mnemonic is any text within the instruction that
controls the function of the instruction, but which is disjoint from
the primary mnemonic (which will have styled `STYLE_MNEMONIC`).

As an example, consider this AArch64 instruction:

```
add	w16, w7, w1, lsl #1
```

The `add` is the primary instruction mnemonic, and would be given
style `STYLE_MNEMONIC`, while `lsl` is the sub-mnemonic, and
would be given the style `STYLE_SUB_MNEMONIC`.

GDB styles text with this style using the `disassembler
mnemonic` style setting.

`gdb.disassembler.STYLE_ASSEMBLER_DIRECTIVE`

Sometimes a series of bytes doesn’t decode to a valid instruction. In
this case the disassembler may choose to represent the result of
disassembling using an assembler directive, for example:

```
.word	0x1234
```

In this case, the `.word` would be give the
`STYLE_ASSEMBLER_DIRECTIVE` style. An assembler directive is
similar to a mnemonic in many ways but is something that is not part
of the architecture’s instruction set.

GDB styles text with this style using the `disassembler
mnemonic` style setting.

`gdb.disassembler.STYLE_REGISTER`

This style is used for styling any text that represents a register
name, or register number, within a disassembled instruction.

GDB styles text with this style using the `disassembler
register` style setting.

`gdb.disassembler.STYLE_ADDRESS`

This style is used for styling numerical values that represent
absolute addresses within the disassembled instruction.

When creating a `DisassemblerTextPart` with this style, you
should consider if a `DisassemblerAddressPart` would be more
appropriate. See [Disassembler Styling Parts](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#Disassembler-Styling-Parts) for a description
of what each part offers.

GDB styles text with this style using the `disassembler
address` style setting.

`gdb.disassembler.STYLE_ADDRESS_OFFSET`

This style is used for styling numerical values that represent offsets
to addresses within the disassembled instruction. A value is
considered an address offset when the instruction itself is going to
access memory, and the value is being used to offset which address is
accessed.

For example, an architecture might have an instruction that loads from
memory using an address within a register. If that instruction also
allowed for an immediate offset to be encoded into the instruction,
this would be an address offset. Similarly, a branch instruction
might jump to an address in a register plus an address offset that is
encoded into the instruction.

GDB styles text with this style using the `disassembler
immediate` style setting.

`gdb.disassembler.STYLE_IMMEDIATE`

Use `STYLE_IMMEDIATE` for any numerical values within a
disassembled instruction when those values are not addresses, address
offsets, or register numbers (The styles `STYLE_ADDRESS`,
`STYLE_ADDRESS_OFFSET`, or `STYLE_REGISTER` can be used in
those cases).

GDB styles text with this style using the `disassembler
immediate` style setting.

`gdb.disassembler.STYLE_SYMBOL`

This style is used for styling the textual name of a symbol that is
included within a disassembled instruction. A symbol name is often
included next to an absolute address within a disassembled instruction
to make it easier for the user to understand what the address is
referring too. For example:

```
call   0x401136 <foo>
```

Here `foo` is the name of a symbol, and should be given the
`STYLE_SYMBOL` style.

Adding symbols next to absolute addresses like this is handled
automatically by the `DisassemblerAddressPart` class
(see [Disassembler Styling Parts](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Disassembly-In-Python.html#Disassembler-Styling-Parts)).

GDB styles text with this style using the `disassembler
symbol` style setting.

`gdb.disassembler.STYLE_COMMENT_START`

This style is used to start a line comment in the disassembly output.
Unlike other styles, which only apply to the single
`DisassemblerTextPiece` to which they are applied, the comment
style is sticky, and overrides the style of any further pieces within
this instruction.

This means that, after a `STYLE_COMMENT_START` piece has been
seen, GDB will apply the comment style until the end of the
line, ignoring the specific style within a piece.

GDB styles text with this style using the `disassembler
comment` style setting.

The following functions are also contained in the
`gdb.disassembler` module:

Function: **register\_disassembler** _(disassembler, architecture)_

The disassembler must be a sub-class of
`gdb.disassembler.Disassembler` or `None`.

The optional architecture is either a string, or the value
`None`. If it is a string, then it should be the name of an
architecture known to GDB, as returned either from
`gdb.Architecture.name`
(see [gdb.Architecture.name](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Architectures-In-Python.html#gdbpy_005farchitecture_005fname)), or from
`gdb.architecture_names`
(see [gdb.architecture\_names](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Basic-Python.html#gdb_005farchitecture_005fnames)).

The disassembler will be installed for the architecture named by
architecture, or if architecture is `None`, then
disassembler will be installed as a global disassembler for use
by all architectures.

GDB only records a single disassembler for each architecture,
and a single global disassembler. Calling
`register_disassembler` for an architecture, or for the global
disassembler, will replace any existing disassembler registered for
that architecture value. The previous disassembler is returned.

If disassembler is `None` then any disassembler currently
registered for architecture is deregistered and returned.

When GDB is looking for a disassembler to use, GDB
first looks for an architecture specific disassembler. If none has
been registered then GDB looks for a global disassembler (one
registered with architecture set to `None`). Only one
disassembler is called to perform disassembly, so, if there is both an
architecture specific disassembler, and a global disassembler
registered, it is the architecture specific disassembler that will be
used.

GDB tracks the architecture specific, and global
disassemblers separately, so it doesn’t matter in which order
disassemblers are created or registered; an architecture specific
disassembler, if present, will always be used in preference to a
global disassembler.

You can use the `maint info python-disassemblers` command
(see [maint info python-disassemblers](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Maintenance-Commands.html#maint-info-python_002ddisassemblers)) to see which disassemblers
have been registered.

Function: **builtin\_disassemble** _(info)_

This function calls back into GDB’s builtin disassembler to
disassemble the instruction identified by info, an instance, or
sub-class, of `DisassembleInfo`.

When the builtin disassembler needs to read memory the
`read_memory` method on info will be called. By
sub-classing `DisassembleInfo` and overriding the
`read_memory` method, it is possible to intercept calls to
`read_memory` from the builtin disassembler, and to modify the
values returned.

It is important to understand that, even when
`DisassembleInfo.read_memory` raises a `gdb.MemoryError`, it
is the internal disassembler itself that reports the memory error to
GDB. The reason for this is that the disassembler might
probe memory to see if a byte is readable or not; if the byte can’t be
read then the disassembler may choose not to report an error, but
instead to disassemble the bytes that it does have available.

If the builtin disassembler is successful then an instance of
`DisassemblerResult` is returned from `builtin_disassemble`,
alternatively, if something goes wrong, an exception will be raised.

A `MemoryError` will be raised if `builtin_disassemble` is
unable to read some memory that is required in order to perform
disassembly correctly.

Any exception that is not a `MemoryError`, that is raised in a
call to `read_memory`, will pass through
`builtin_disassemble`, and be visible to the caller.

Finally, there are a few cases where GDB’s builtin
disassembler can fail for reasons that are not covered by
`MemoryError`. In these cases, a `GdbError` will be raised.
The contents of the exception will be a string describing the problem
the disassembler encountered.

Here is an example that registers a global disassembler. The new
disassembler invokes the builtin disassembler, and then adds a
comment, `## Comment`, to each line of disassembly output:

```
class ExampleDisassembler(gdb.disassembler.Disassembler):
    def __init__(self):
        super().__init__("ExampleDisassembler")

    def __call__(self, info):
        result = gdb.disassembler.builtin_disassemble(info)
        length = result.length
        text = result.string + "\t## Comment"
        return gdb.disassembler.DisassemblerResult(length, text)

gdb.disassembler.register_disassembler(ExampleDisassembler())
```

The following example creates a sub-class of `DisassembleInfo` in
order to intercept the `read_memory` calls, within
`read_memory` any bytes read from memory have the two 4-bit
nibbles swapped around. This isn’t a very useful adjustment, but
serves as an example.

```
class MyInfo(gdb.disassembler.DisassembleInfo):
    def __init__(self, info):
        super().__init__(info)

    def read_memory(self, length, offset):
        buffer = super().read_memory(length, offset)
        result = bytearray()
        for b in buffer:
            v = int.from_bytes(b, 'little')
            v = (v << 4) & 0xf0 | (v >> 4)
            result.append(v)
        return memoryview(result)

class NibbleSwapDisassembler(gdb.disassembler.Disassembler):
    def __init__(self):
        super().__init__("NibbleSwapDisassembler")

    def __call__(self, info):
        info = MyInfo(info)
        return gdb.disassembler.builtin_disassemble(info)

gdb.disassembler.register_disassembler(NibbleSwapDisassembler())
```

## 23.3.2.43 Missing Debug Info In Python

When GDB encounters a new objfile (see [Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfiles-In-Python)), e.g. the primary executable, or any shared libraries used
by the inferior, GDB will attempt to load the corresponding
debug information for that objfile. The debug information might be
found within the objfile itself, or within a separate objfile which
GDB will automatically locate and load.

Sometimes though, GDB might not find any debug information
for an objfile, in this case the debugging experience will be
restricted.

If GDB fails to locate any debug information for a particular
objfile, there is an opportunity for a Python extension to step in. A
Python extension can potentially locate the missing debug information
using some platform- or project-specific steps, and inform
GDB of its location. Or a Python extension might provide
some platform- or project-specific advice to the user about how to
obtain the missing debug information.

A missing debug information Python extension consists of a handler
object which has the `name` and `enabled` attributes, and
implements the `__call__` method. When GDB encounters
an objfile for which it is unable to find any debug information, it
invokes the `__call__` method. Full details of how handlers are
written can be found below.

#### The `gdb.missing_debug` Module

GDB comes with a `gdb.missing_debug` module which
contains the following class and global function:

class: **gdb.missing\_debug.MissingDebugHandler**

`MissingDebugHandler` is a base class from which user-created
handlers can derive, though it is not required that handlers derive
from this class, so long as any user created handler has the
`name` and `enabled` attributes, and implements the
`__call__` method.

Function: **MissingDebugHandler.\_\_init\_\_** _(name)_

The name is a string used to reference this missing debug
handler within some GDB commands. Valid names consist of the
characters `[-_a-zA-Z0-9]`, creating a handler with an invalid
name raises a `ValueError` exception.

Function: **MissingDebugHandler.\_\_call\_\_** _(objfile)_

Sub-classes must override the `__call__` method. The
objfile argument will be a `gdb.Objfile`, this is the
objfile for which GDB was unable to find any debug
information.

The return value from the `__call__` method indicates what
GDB should do next. The possible return values are:

- `None`
  This indicates that this handler could not help with objfile,
  GDB should call any other registered handlers.

- `True`
  This indicates that this handler has installed the debug information
  into a location where GDB would normally expect to find it
  when looking for separate debug information files (see [Separate Debug Files](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Separate-Debug-Files.html#Separate-Debug-Files)). GDB will repeat the normal lookup process,
  which should now find the separate debug file.


  If GDB still doesn’t find the separate debug information file
  after this second attempt, then the Python missing debug information
  handlers are not invoked a second time, this prevents a badly behaved
  handler causing GDB to get stuck in a loop. GDB
  will continue without any debug information for objfile.

- `False`
  This indicates that this handler has done everything that it intends
  to do with objfile, but no separate debug information can be
  found. GDB will not call any other registered handlers for
  objfile. GDB will continue without debugging
  information for objfile.

- A string


  The returned string should contain a filename. GDB will not
  call any further registered handlers, and will instead load the debug
  information from the file identified by the returned filename.


Invoking the `__call__` method from this base class will raise a
`NotImplementedError` exception.

Variable: **MissingDebugHandler.name**

A read-only attribute which is a string, the name of this handler
passed to the `__init__` method.

Variable: **MissingDebugHandler.enabled**

A modifiable attribute containing a boolean; when `True`, the
handler is enabled, and will be used by GDB. When
`False`, the handler has been disabled, and will not be used.

Function: **gdb.missing\_debug.register\_handler** _(locus, handler, replace=`False`)_

Register a new missing debug handler with GDB.

handler is an instance of a sub-class of
`MissingDebugHandler`, or at least an instance of an object that
has the same attributes and methods as `MissingDebugHandler`.

locus specifies to which handler list to prepend handler.
It can be either a `gdb.Progspace` (see [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python))
or `None`, in which case the handler is registered globally. The
newly registered handler will be called before any other handler
from the same locus. Two handlers in the same locus cannot have the
same name, an attempt to add a handler with an already existing name
raises an exception unless replace is `True`, in which case
the old handler is deleted and the new handler is prepended to the
selected handler list.

GDB first calls the handlers for the current program space,
and then the globally registered handlers. As soon as a handler
returns a value other than `None`, no further handlers are called
for this objfile.

## 23.3.2.44 Missing Objfiles In Python

When GDB opens a core file, for example with the
`core-file` command (see [core-file command](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Files.html#core_002dfile-command)), GDB will
attempt to load the corresponding executable and shared libraries.
Often these files can be found on the local machine, but sometimes
these files cannot be found, in which case the debugging experience
will be restricted.

If GDB fails to locate a particular file then there is an
opportunity for a Python extension to step in. A Python extension can
potentially locate the missing file using some platform- or
project-specific steps, and inform GDB of its location. Or a
Python extension might provide some platform- or project-specific
advice to the user about how to obtain the missing file.

A missing-objfile Python extension consists of a handler object which
has the `name` and `enabled` attributes, and implements the
`__call__` method. When GDB encounters a situation
where a file cannot be found, but the build-id (see [build ID](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Separate-Debug-Files.html#build-ID)) for
the missing file is known, then the `__call__` method is invoked
to try and find the file. Full details of how handlers are written
can be found below.

#### The `gdb.missing_objfile` Module

GDB comes with a `gdb.missing_objfile` module which
contains the following class and global function:

class: **gdb.missing\_objfile.MissingObjfileHandler**

`MissingObjfileHandler` is a base class from which user-created
handlers can derive, though it is not required that handlers derive
from this class, so long as any user created handler has the
`name` and `enabled` attributes, and implements the
`__call__` method.

Function: **MissingObjfileHandler.\_\_init\_\_** _(name)_

The name is a string used to reference this missing-objfile
handler within some GDB commands. Valid names consist of the
characters ‘ `[-_a-zA-Z0-9]`’, creating a handler with an invalid
name raises a `ValueError` exception.

Function: **MissingObjfileHandler.\_\_call\_\_** _(pspace, build\_id, filename)_

Sub-classes must override the `__call__` method. The
pspace argument will be a `gdb.Progspace`
(see [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python)), this is the program space in which
GDB is looking for the missing file.

The build\_id argument is a string containing the build-id of the
file that is missing, this will be in the same format as returned by
`Objfile.build_id` (see [Objfile.build\_id](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfile_002ebuild_005fid)).

The filename argument contains the name of the file that
GDB is looking for. This information is provided to allow
handlers to generate informative messages for the user. A handler is
not required to place the missing file at this location. There might
already be a file present at this location, but it might not match the
required build-id, in which case GDB will have ignored it.
In some limited cases GDB might not be able to establish the
filename of the file it is searching for, in this case
GDB will use a string ‘ `with build-id build_id`’ as a
replacement.

The return value from the `__call__` method indicates what
GDB should do next. The possible return values are:

- `None`
  This indicates that this handler could not locate the missing file and
  GDB should call any other registered handlers.

- `True`
  This indicates that this handler has installed the missing file into a
  location where GDB would normally expect to find it. The
  only location in which GDB will look is within the
  `.build-id` sub-directory within the debug-file-directory
  (see [debug-file-directory](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Separate-Debug-Files.html#debug_002dfile_002ddirectory)).


  GDB will repeat the normal lookup process, which should now
  find the previously missing file.


  If GDB still doesn’t find file after this second attempt,
  then the Python missing-objfile handlers are not invoked a second
  time, this prevents a badly behaved handler causing GDB to
  get stuck in a loop. GDB will continue without the missing
  file, though this will degrade the debugging experience.

- `False`
  This indicates that this handler has done everything that it intends
  to do but the missing file could not be found. GDB will not
  call any other registered handlers to look for the missing file.
  GDB will continue without the missing file, though this will
  degrade the debugging experience.

- A string


  The returned string should contain a filename. GDB will not
  call any further registered handlers, and will instead use the
  returned filename as the missing file.

Invoking the `__call__` method from this base class will raise a
`NotImplementedError` exception.

Variable: **MissingObjfileHandler.name**

A read-only attribute which is a string, the name of this handler
passed to the `__init__` method.

Variable: **MissingObjfileHandler.enabled**

A modifiable attribute containing a boolean; when `True`, the
handler is enabled, and will be used by GDB. When
`False`, the handler has been disabled, and will not be used.

Function: **gdb.missing\_objfile.register\_handler** _(locus, handler, replace=`False`)_

Register a new missing-objfile handler with GDB.

handler is an instance of a sub-class of
`MissingObjfileHandler`, or at least an instance of an object that
has the same attributes and methods as `MissingObjfileHandler`.

locus specifies to which handler list to prepend handler.
It can be either a `gdb.Progspace` (see [Progspaces In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Progspaces-In-Python.html#Progspaces-In-Python))
or `None`, in which case the handler is registered globally. The
newly registered handler will be called before any other handler
from the same locus. Two handlers in the same locus cannot have the
same name, an attempt to add a handler with an already existing name
raises an exception unless replace is `True`, in which case
the old handler is deleted and the new handler is prepended to the
selected handler list.

GDB first calls the handlers for the current program space,
and then the globally registered handlers. As soon as a handler
returns a value other than `None`, no further handlers are
called.

#### Managing Missing-Objfile Handlers

GDB defines the following commands to manage registered
missing-objfile handlers:

`info missing-objfile-handlers [ locus [ name-regexp ] ]`

Lists all registered missing-objfile handlers. Arguments locus
and name-regexp are both optional and can be used to filter
which handlers are listed.

The locus argument should be either `global`,
`progspace`, or the name of an object file. Only handlers
registered for the specified locus will be listed.

The name-regexp is a regular expression used to match against
handler names.

`disable missing-objfile-handler [ locus [ name-regexp ] ]`

The locus and name-regexp are interpreted as in `info
missing-objfile-handlers` above, but instead of listing the matching
handlers, all of the matching handlers are disabled. The
`enabled` field of each matching handler is set to `False`.

`enable missing-objfile-handler [ locus [ name-regexp ] ]`

The locus and name-regexp are interpreted as in `info
missing-objfile-handlers` above, but instead of listing the matching
handlers, all of the matching handlers are enabled. The
`enabled` field of each matching handler is set to `True`.

## 23.3.2.45 Core Files In Python

When a core file is loaded into an inferior (see [Inferiors In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#Inferiors-In-Python)) for examination (see [core-file command](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Files.html#core_002dfile-command)), information
about the core file is contained in a `gdb.Corefile` object.

The `gdb.Corefile` for an inferior can be accessed using the
`Inferior.corefile` attribute. This will be `None` if
no core file is loaded.

A `gdb.Corefile` object has the following attributes:

Variable: **Corefile.filename**

This read only attribute contains a non-empty string, the file name of
the core file. Attempting to access this attribute on an invalid
`gdb.Corefile` object will raise a `RuntimeError` exception.

A `gdb.Corefile` object has the following methods:

Function: **Corefile.is\_valid** _()_

Returns `True` if the `gdb.Corefile` object is valid,
`False` if not. A `gdb.Corefile` object will become invalid
when the core file is unloaded from the inferior using the
`core-file` command (see [core-file command](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Files.html#core_002dfile-command)), or if the
inferior in which the core file is loaded is deleted. All other
`gdb.Corefile` methods and attributes will throw an exception if
it is invalid at the time the method is called, or the attribute
accessed.

Function: **Corefile.mapped\_files** _()_

Return a list of `gdb.CorefileMappedFile` (see below) objects
representing files that were mapped into the process when the core
file was created. This information is read from the ‘ `NT_FILE`’
core file note on Linux. Not every target supports accessing this
information, for targets without support, an empty list will be
returned.

One may add arbitrary attributes to `gdb.Corefile` objects in the
usual Python way. This is useful if, for example, one needs to do
some extra record keeping associated with the corefile.
See [choosing attribute names](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Inferiors-In-Python.html#choosing-attribute-names), for guidance on selecting a suitable
name for new attributes.

The `Corefile.mapped_files ()` method returns a list of
`gdb.CorefileMappedFile` objects. Each of these objects
represents a file that was fully, or partially, mapped into the
processes address space when the core file was created.

A `gdb.CorefileMappedFile` object has the following attributes:

Variable: **CorefileMappedFile.filename**

This read only attribute contains a string, the file name of the
mapped file. This string can be empty if GDB was unable to
find the name of the mapped file. If this string is empty then
`CorefileMappedFile.build_id` will not be `None`.

Variable: **CorefileMappedFile.build\_id**

This read only attribute contains a non-empty string or `None`.
This is the build-id of the mapped file extracted from the core file,
or `None` if there was no build-id, or GDB was unable to
extract the build-id.

Variable: **CorefileMappedFile.is\_main\_executable**

This read only attribute is `True` if GDB believes this
mapping represents the main executable for which this core file was
created. This will be `False` for all other mappings.

Variable: **CorefileMappedFile.regions**

This read only attribute contains a list of
`gdb.CorefileMappedFileRegion` objects. Each of these objects
describes a region of the file that was mapped into the process when
the core file was created, further details are given below.

The `gdb.CorefileMappedFileRegion` object describes which part of
a file that was mapped into a process when the core file was created.

A `gdb.CorefileMappedFile` object has the following attributes:

Variable: **CorefileMappedFileRegion.start**

This read only attribute contains the start address of this mapping
within the inferior.

Variable: **CorefileMappedFileRegion.end**

This read only attribute contains end address of this mapping within
the inferior.

Variable: **CorefileMappedFileRegion.file\_offset**

This read only attribute contains the offset within the mapped file
for this mapping. This attribute will be `0` if the containing
`CorefileMappedFile.filename` is empty, in which case this
attribute has no meaning.

## 23.3.3 Python Auto-loading

When a new object file is read (for example, due to the `file`
command, or because the inferior has loaded a shared library),
GDB will look for Python support scripts in several ways:
`objfile-gdb.py` and `.debug_gdb_scripts` section.
See [Auto-loading extensions](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Auto_002dloading-extensions.html#Auto_002dloading-extensions).

The auto-loading feature is useful for supplying application-specific
debugging commands and scripts.

Auto-loading can be enabled or disabled,
and the list of auto-loaded scripts can be printed.

`set auto-load python-scripts [on|off]`

Enable or disable the auto-loading of Python scripts.

`show auto-load python-scripts`

Show whether auto-loading of Python scripts is enabled or disabled.

`info auto-load python-scripts [regexp]`

Print the list of all Python scripts that GDB auto-loaded.

Also printed is the list of Python scripts that were mentioned in
the `.debug_gdb_scripts` section and were either not found
(see [dotdebug\_gdb\_scripts section](https://sourceware.org/gdb/current/onlinedocs/gdb.html/dotdebug_005fgdb_005fscripts-section.html#dotdebug_005fgdb_005fscripts-section)) or were not auto-loaded due to
`auto-load safe-path` rejection (see [Auto-loading](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Auto_002dloading.html#Auto_002dloading)).
This is useful because their names are not printed when GDB
tries to load them and fails. There may be many of them, and printing
an error message for each one is problematic.

If regexp is supplied only Python scripts with matching names are printed.

Example:

```text
(gdb) info auto-load python-scripts
Loaded Script
Yes    py-section-script.py
       full name: /tmp/py-section-script.py
No     my-foo-pretty-printers.py
```

When reading an auto-loaded file or script, GDB sets the
_current objfile_. This is available via the `gdb.current_objfile`
function (see [Objfiles In Python](https://sourceware.org/gdb/current/onlinedocs/gdb.html/Objfiles-In-Python.html#Objfiles-In-Python)). This can be useful for
registering objfile-specific pretty-printers and frame-filters.

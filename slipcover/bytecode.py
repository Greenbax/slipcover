from __future__ import annotations
import sys
import dis
import types
from typing import List

PYTHON_VERSION = sys.version_info[0:2]

# FIXME provide __all__

# Python 3.10a7 changed branch opcodes' argument to mean instruction
# (word) offset, rather than bytecode offset.
if PYTHON_VERSION >= (3,10):
    def offset2branch(offset: int) -> int:
        assert offset % 2 == 0
        return offset//2

    def branch2offset(arg: int) -> int:
        return arg*2
else:
    def offset2branch(offset: int) -> int:
        return offset

    def branch2offset(arg: int) -> int:
        return arg


op_EXTENDED_ARG = dis.EXTENDED_ARG
is_EXTENDED_ARG = [op_EXTENDED_ARG]
op_LOAD_CONST = dis.opmap["LOAD_CONST"]
op_LOAD_GLOBAL = dis.opmap["LOAD_GLOBAL"]

if PYTHON_VERSION >= (3,11):
    op_PUSH_NULL = dis.opmap["PUSH_NULL"]
    op_PRECALL = dis.opmap["PRECALL"]
    op_CALL = dis.opmap["CALL"]
    op_CACHE = dis.opmap["CACHE"]
    is_EXTENDED_ARG.append(dis._all_opmap["EXTENDED_ARG_QUICK"])
else:
    op_PUSH_NULL = None
    op_CALL_FUNCTION = dis.opmap["CALL_FUNCTION"]

op_POP_TOP = dis.opmap["POP_TOP"]
op_JUMP_FORWARD = dis.opmap["JUMP_FORWARD"]
op_NOP = dis.opmap["NOP"]


def arg_ext_needed(arg: int) -> int:
    """Returns the number of EXTENDED_ARGs needed for an argument."""
    return (arg.bit_length() - 1) // 8


def opcode_arg(opcode: int, arg: int, min_ext : int = 0) -> List[int]:
    """Emits an opcode and its (variable length) argument."""
    bytecode = []
    ext = max(arg_ext_needed(arg), min_ext)
    assert ext <= 3
    for i in range(ext):
        bytecode.extend(
            [op_EXTENDED_ARG, (arg >> (ext - i) * 8) & 0xFF]
        )
    bytecode.extend([opcode, arg & 0xFF])
    if PYTHON_VERSION >= (3,11):
        bytecode.extend([op_CACHE, 0] * dis._inline_cache_entries[opcode])
    return bytecode


def unpack_opargs(code: bytes) -> List[(int, int, int, int)]:
    """Unpacks opcodes and their arguments, returning:

    - the beginning offset, including that of the first EXTENDED_ARG, if any
    - the length (offset + length is where the next opcode starts)
    - the opcode
    - its argument (decoded)
    """
    ext_arg = 0
    next_off = 0
    off = 0
    while off < len(code):
        op = code[off]
        if op in is_EXTENDED_ARG:
            ext_arg = (ext_arg | code[off+1]) << 8
        else:
            arg = (ext_arg | code[off+1])
            if PYTHON_VERSION >= (3,11):
                while off+2 < len(code) and code[off+2] == op_CACHE:
                    off += 2
            yield (next_off, off+2-next_off, op, arg)
            ext_arg = 0
            next_off = off+2
        off += 2


def calc_max_stack(code: bytes) -> int:
    """Calculates the maximum stack size for code to execute.

    Assumes linear execution (i.e., not things like a loop pushing to the stack).
    """
    max_stack = stack = 0
    for (_, _, op, arg) in unpack_opargs(code):
        stack += dis.stack_effect(op, arg if op >= dis.HAVE_ARGUMENT else None)
        max_stack = max(stack, max_stack)

    return max_stack


class Branch:
    """Describes a branch instruction."""

    def __init__(self, offset : int, length : int, opcode : int, arg : int):
        """Initializes a new Branch.

        offset - offset in code where the instruction starts; if EXTENDED_ARGs are
            used, it should be the offset of the first EXTENDED_ARG
        length - instruction length, including that of any EXTENDED_ARGs
        opcode - the instruction's opcode
        arg - the instruction's argument (decoded if using EXTENDED_ARGs)
        """
        self.offset = offset
        self.length = length
        self.opcode = opcode
        self.is_relative = (opcode in dis.hasjrel)
        self.is_backward = 'JUMP_BACKWARD' in dis.opname[opcode]
        self.target = branch2offset(arg) if not self.is_relative \
                      else offset + length + branch2offset(-arg if self.is_backward else arg)

    def arg(self) -> int:
        """Returns this branch's opcode argument."""
        if self.is_relative:
            return offset2branch(abs(self.target - (self.offset + self.length)))
        return offset2branch(self.target)

    def adjust(self, insert_offset : int, insert_length : int) -> None:
        """Adjusts this branch after a code insertion."""
        if self.offset >= insert_offset:
            self.offset += insert_length
        if self.target > insert_offset:
            self.target += insert_length

    def adjust_length(self) -> int:
        """Adjusts this branch's opcode length, if needed.

        Returns the number of bytes by which the length increased.
        """
        length_needed = 2 + 2*arg_ext_needed(self.arg())
        change = max(0, length_needed - self.length)
        if change:
            if self.target > self.offset:
                self.target += change
            self.length = length_needed

        return change

    def code(self) -> bytes:
        """Emits this branch's code."""
        assert self.length >= 2 + 2*arg_ext_needed(self.arg())
        return opcode_arg(self.opcode, self.arg(), (self.length-2)//2)

    @staticmethod
    def from_code(code : types.CodeType) -> List[Branch]:
        """Finds all Branches in code."""
        branches = []

        branch_opcodes = set(dis.hasjrel).union(dis.hasjabs)

        for (off, length, op, arg) in unpack_opargs(code.co_code):
            if op in branch_opcodes:
                branches.append(Branch(off, length, op, arg))

        return branches


def append_varint(data, n):
    """Appends a (little endian) variable length unsigned integer to 'data'"""
    while n > 0x3f:
        data.append(0x40|(n&0x3f))
        n = n >> 6
    data.append(n)
    return data


def append_svarint(data, n):
    """Appends a (little endian) variable length signed integer to 'data'"""
    return append_varint(data, ((-n)<<1)|1 if n < 0 else n<<1)


def write_varint_be(n, mark_first=None):
    """Encodes a (big endian) variable length unsigned integer"""
    data = bytearray()
    top_bit = n.bit_length()-1
    for shift in range(top_bit - top_bit%6, 0, -6):
        data.append(0x40|((n >> shift)&0x3f))
    data.append(n&0x3f)
    if mark_first:
        data[0] |= mark_first
    return data


def read_varint_be(it):
    """Decodes a (big endian) variable length unsigned integer from 'it'"""
    value = 0
    while (b := next(it)) & 0x40:
        value |= b & 0x3f
        value <<= 6
    value |= b & 0x3f

    return value


class ExceptionTableEntry:
    """Represents an entry from Python 3.11+'s exception table."""
    def __init__(self, start: int, end: int, target: int, other: int):
        self.start = start
        self.end = end
        self.target = target
        self.other = other


    # FIXME tests missing
    def adjust(self, insert_offset: int, insert_length: int) -> None:
        """Adjusts this exception table entry, handling a code insertion."""
        old_start, old_end, old_target = self.start, self.end, self.target
        if insert_offset <= self.start:
            self.start += insert_length
        if insert_offset < self.end:
            self.end += insert_length
        if insert_offset < self.target:
            self.target += insert_length
#        print(f"{old_start}-{old_end}->{old_target} ==> {self.start}-{self.end}->{self.target}")


    @staticmethod
    def from_code(code: types.CodeType) -> List[ExceptionTableEntry]:
        """Returns a list of exception table entries from a code object."""
        entries = []
        it = iter(code.co_exceptiontable)
        try:
            while True:
                start = branch2offset(read_varint_be(it))
                length = branch2offset(read_varint_be(it))
                end = start + length
                target = branch2offset(read_varint_be(it))
                other = read_varint_be(it)
                entries.append(ExceptionTableEntry(start, end, target, other))
        except StopIteration:
#            for e in entries:
#                print(f"{e.start}-{e.end}: {e.target}")
            return entries


    @staticmethod
    def make_exceptiontable(entries: List[ExceptionTableEntry]) -> bytes:
        """Generates an exception table from a list of entries."""
        table = bytearray()

        for e in entries:
            table.extend(write_varint_be(offset2branch(e.start), mark_first=0x80))
            table.extend(write_varint_be(offset2branch(e.end - e.start)))
            table.extend(write_varint_be(offset2branch(e.target)))
            table.extend(write_varint_be(e.other))

        return bytes(table)


class LineEntry:
    def __init__(self, start : int, end : int, number : int):
        """Initializes a new line entry.

        start, end: start and end offsets in the code
        number: line number
        """
        self.start = start
        self.end = end
        self.number = number

    # FIXME tests missing
    def adjust(self, insert_offset : int, insert_length : int) -> None:
        """Adjusts this line after a code insertion."""
        if self.start > insert_offset:  # note this may extend/shrink the line
            self.start += insert_length
        if self.end > insert_offset:
            self.end += insert_length

    @staticmethod
    def from_code(code : types.CodeType) -> List[LineEntry]:
        def gen_lines():
            last = None
            for line in dis.findlinestarts(code):
                if last is not None:
                    yield LineEntry(last[0], line[0], last[1])
                last = line
            if last is not None:
                yield LineEntry(last[0], len(code.co_code), last[1])

        return [*gen_lines()]

    def __str__(self):
        return f"{self.start}-{self.end}: {self.number}"

    @staticmethod
    def make_lnotab(firstlineno : int, lines : List[LineEntry]) -> bytes:
        """Generates the line number table used by Python 3.9- to map offsets to line numbers."""

        lnotab = []

        prev_start = 0
        prev_number = firstlineno

        for l in lines:
            delta_start = l.start - prev_start
            delta_number = l.number - prev_number

            while delta_start > 255:
                lnotab.extend([255, 0])
                delta_start -= 255

            while delta_number > 127:
                lnotab.extend([delta_start, 127])
                delta_start = 0
                delta_number -= 127

            while delta_number < -128:
                lnotab.extend([delta_start, -128 & 0xFF])
                delta_start = 0
                delta_number += 128

            if delta_start or delta_number:
                lnotab.extend([delta_start, delta_number & 0xFF])

            prev_start = l.start
            prev_number = l.number

        return bytes(lnotab)


    @staticmethod
    def make_linetable(firstlineno : int, lines : List[LineEntry]) -> bytes:
        """Generates the line number table used by Python 3.10 to map offsets to line numbers."""

        linetable = []

        prev_end = 0
        prev_number = firstlineno

        for l in lines:
            gap = l.start - prev_end if l.number is not None else l.end - prev_end

            if gap:
                while gap > 254:
                    linetable.extend([254, -128 & 0xFF])
                    gap -= 254

                linetable.extend([gap, -128 & 0xFF])
                prev_end += gap

                if l.number is None:
                    continue

            delta_end = l.end - prev_end
            delta_number = l.number - prev_number

            while delta_number > 127:
                linetable.extend([0, 127])
                delta_number -= 127

            while delta_number < -127:
                linetable.extend([0, -127 & 0xFF])
                delta_number += 127

            while delta_end > 254:
                linetable.extend([254, delta_number & 0xFF])
                delta_number = 0
                delta_end -= 254

            linetable.extend([delta_end, delta_number & 0xFF])
            prev_number = l.number

            prev_end = l.end

        return bytes(linetable)


    @staticmethod
    def make_positions(firstlineno : int, lines : List[LineEntry]) -> bytes:
        """Generates the positions table used by Python 3.11+ to map offsets to line numbers."""

        linetable = []

        prev_end = 0
        prev_number = firstlineno

        for l in lines:
#            print(f"{l.start} {l.end} {l.number}")

            if l.number is None:
                bytecodes = (l.end - prev_end)//2
                while bytecodes > 0:
#                    print(f"->15 {min(bytecodes, 8)-1}")
                    linetable.extend([0x80|(15<<3)|(min(bytecodes, 8)-1)])
                    bytecodes -= 8
            else:
                if prev_end < l.start:
                    bytecodes = (l.start - prev_end)//2
                    while bytecodes > 0:
#                        print(f"->15 {min(bytecodes, 8)-1}")
                        linetable.extend([0x80|(15<<3)|(min(bytecodes, 8)-1)])
                        bytecodes -= 8

                line_delta = l.number - prev_number
                bytecodes = (l.end - l.start)//2
                while bytecodes > 0:
#                    print(f"->13 {min(bytecodes, 8)-1} {line_delta}")
                    linetable.extend([0x80|(13<<3)|(min(bytecodes, 8)-1)])
                    append_svarint(linetable, line_delta)
                    line_delta = 0
                    bytecodes -= 8

                prev_number = l.number

            prev_end = l.end

        return bytes(linetable)


class Editor:
    def __init__(self, code):
        self.orig_code = code

        self.consts = list(code.co_consts)
        self.branches = Branch.from_code(code)
        self.ex_table = ExceptionTableEntry.from_code(code) if PYTHON_VERSION >= (3,11) else []
        self.lines = LineEntry.from_code(code)

        self.patch = bytearray(code.co_code)
        self.max_addtl_stack = 0
        self.finished = False


    def add_const(self, value):
        self.consts.append(value)
        return len(self.consts)-1


    def insert_function_call(self, offset, function, args):
        assert isinstance(function, int)    # we only support const references so far

        insert = bytearray()

        if PYTHON_VERSION >= (3,11):
            insert.extend([op_NOP, 0,    # for deinstrument jump
                           op_PUSH_NULL, 0] +
                          opcode_arg(op_LOAD_CONST, function))

            for a in args:
                insert.extend(opcode_arg(op_LOAD_CONST, a))


            insert.extend(opcode_arg(op_PRECALL, len(args)) +
                          opcode_arg(op_CALL, len(args)) +
                          [op_POP_TOP, 0])   # ignore return
        else:
            insert.extend([op_NOP, 0] +  # for deinstrument jump
                          opcode_arg(op_LOAD_CONST, function))

            for a in args:
                insert.extend(opcode_arg(op_LOAD_CONST, a))

            insert.extend([op_CALL_FUNCTION, len(args),
                           op_POP_TOP, 0])   # ignore return

        len_insert = len(insert)

        insert[1] = offset2branch(len_insert-2)    # fails if > 255
        self.max_addtl_stack = max(self.max_addtl_stack, calc_max_stack(insert))

        self.patch[offset:offset] = insert

        for l in self.lines:
            l.adjust(offset, len_insert)

        for b in self.branches:
            b.adjust(offset, len_insert)

        for e in self.ex_table:
            e.adjust(offset, len_insert)

        return len_insert


    def replace_global_with_const(self, global_name, const_index):
        if global_name in self.orig_code.co_names:
            name_index = self.orig_code.co_names.index(global_name)

            def find_load_globals():
                for op_off, op_len, op, op_arg in unpack_opargs(self.patch):
                    if op == op_LOAD_GLOBAL:
                        if PYTHON_VERSION >= (3,11):
                            if (op_arg>>1) == name_index:
                                yield (op_off, op_len, op, op_arg)
                        else:
                            if op_arg == name_index:
                                yield (op_off, op_len, op, op_arg)

            delta = 0
            # read from pre-computed list so we can modify on the fly
            for op_off, op_len, op, op_arg in list(find_load_globals()):
                repl = bytearray()
                if sys.version_info[0:2] >= (3,11) and op_arg&1:
                    repl.extend(opcode_arg(dis.opmap['PUSH_NULL'], 0))
                repl.extend(opcode_arg(op_LOAD_CONST, const_index))

                op_off += delta     # adjust for any other changes
                self.patch[op_off:op_off+op_len] = repl

                change = len(repl) - op_len
                if change:
                    for l in self.lines:
                        l.adjust(op_off, change)

                    for b in self.branches:
                        b.adjust(op_off, change)

                    for e in self.ex_table:
                        e.adjust(op_off, change)

                delta += change


    def finish(self):
        assert not self.finished

        # A branch's new target may now require more EXTENDED_ARG opcodes to be expressed.
        # Inserting space for those may in turn trigger needing more space for others...
        # FIXME missing test for length adjustment triggering other length adjustments
        any_adjusted = True
        while any_adjusted:
            any_adjusted = False

            for b in self.branches:
                change = b.adjust_length()
                if change:
#                    print(f"adjusted branch {b.offset}->{b.target} by {change} to length={b.length}")
                    self.patch[b.offset:b.offset] = [0] * change
                    for c in self.branches:
                        if b != c:
                            c.adjust(b.offset, change)

                    for l in self.lines:
                        l.adjust(b.offset, change)

                    for e in self.ex_table:
                        e.adjust(b.offset, change)

                    any_adjusted = True

        for b in self.branches:
            assert self.patch[b.offset+b.length-2] == b.opcode
            self.patch[b.offset:b.offset+b.length] = b.code()

        kwargs = {}
        if PYTHON_VERSION < (3,10):
            kwargs["co_lnotab"] = LineEntry.make_lnotab(self.orig_code.co_firstlineno, self.lines)
        elif PYTHON_VERSION == (3,10):
            kwargs["co_linetable"] = LineEntry.make_linetable(self.orig_code.co_firstlineno, self.lines)
        else:
            kwargs["co_linetable"] = LineEntry.make_positions(self.orig_code.co_firstlineno, self.lines)
            kwargs["co_exceptiontable"] = ExceptionTableEntry.make_exceptiontable(self.ex_table)

        self.finished = True

        return self.orig_code.replace(
            co_code=bytes(self.patch),
            co_stacksize=self.orig_code.co_stacksize + self.max_addtl_stack,
            co_consts=tuple(self.consts),
            **kwargs
        )

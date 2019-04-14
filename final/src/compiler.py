#!/usr/bin/env python3
"""Compiler for Go to MIPS."""
from go_classes import *
from intermediate import process_code, inbuilt_funcs, GoException
from argparse import ArgumentParser
import logging
import re
from math import ceil


def _infer_type(item, table):
    """Infer the type from the given string operand and return it."""
    if "[" not in item:
        # Check if it is in this table or in a child scope's table
        for record in table.activation_record:
            if record[0] == item:
                return record[1]
        # Check if it is in this table or in a parent's table
        return table.get_type(item)

    root = re.search(r"[\w_]+", item).group()
    for record in table.activation_record:
        if record[0] == root:
            dtype = record[1]

    remaining = item[item.index("[") :]
    while remaining != "":
        end = remaining.index("]")
        if isinstance(dtype, GoType) and dtype.name != "string":
            # This is most probably a struct
            dtype = table.get_struct_obj(dtype.name)

        if isinstance(dtype, GoArray):
            dtype = dtype.dtype
        elif isinstance(dtype, GoStruct):
            index_val = int(remaining[1:end])
            index = 0
            for struct_field in dtype.vars:
                if index == index_val:
                    dtype = struct_field[1].dtype
                    break
                index += table.get_size(struct_field[1].dtype)
        elif isinstance(dtype, GoType) and dtype.name == "string":
            dtype = GoType("byte", size=1)
        else:
            raise ValueError("What is this?: {}".format(dtype.name))
        remaining = remaining[end + 1 :]
    return dtype


def size2instr(size, mode, is_float=False, is_unsigned=False):
    """Return appropriate instruction for the given size.

    Args:
        dtype (`GoType`): The dtype
        table (`SymbTable`): The symbol table
        mode (str): One of ["load", "store"]
        is_float: Whether the operand is a float
        is_unsigned: Whether the operand is unsigned

    Returns:
        str: The appropriate instruction

    """
    if mode not in ["load", "store"]:
        raise ValueError('Mode must be one of "load" or "store"')

    if is_unsigned and mode == "load":
        suffix = "u"
    else:
        suffix = ""

    if is_float:
        if size == 4:
            return mode[0] + ".s"
        else:
            return mode[0] + ".d"
    else:
        if size == 1:
            return mode[0] + "b" + suffix
        elif size == 2:
            return mode[0] + "h" + suffix
        else:
            return mode[0] + "w"


# TODO: Hardcode printf and scanf
def ir2mips(table, ir_code):
    """Convert 3AC to MIPS assembly using the given symbol table.

    Args:
        table (`SymbTable`): The symbol table
        ir_code (str): The 3AC

    Returns:
        str: The MIPS assembly code for the given 3AC

    """
    # Store global variables in the data segment
    mips = ".data\n"
    global_vars = {}
    for collection in [table.variables, table.intermediates]:
        for var in collection:
            mips += " " * 4 + ".align 2\n"
            mips += " " * 4 + var + ": "
            dtype = collection[var]
            global_vars[var] = dtype
            size = table.get_size(dtype)
            mips += ".space {}\n".format(size)
    mips += " " * 4 + ".align 2\n"
    mips += " " * 4 + '__println_comma: .asciiz " "\n'
    mips += " " * 4 + '__println_newline: .asciiz "\\n"\n'
    mips += "\n"

    mips += ".text\n.globl main\n\n"

    # Get all lines in the global scope
    global_lines = []
    outside_func = True
    ir_lines = []
    for i, line in enumerate(ir_code.strip().split("\n")):
        # This condition kept at the beginning to consider "func begin" as
        # non-global
        if line.startswith("func begin"):
            outside_func = False
        if outside_func:
            global_lines.append(line)
        else:
            ir_lines.append(line)
        # This condition kept at the end to consider "func end" as non-global
        if line.startswith("func end"):
            outside_func = True

    indent = 0

    def get_type(var):
        """Get the type of the variable."""
        if var in global_vars:
            return global_vars[var]
        else:
            return _infer_type(var, table)

    def get_addr(var, reg):
        """Obtain the address at which the variable is stored.

        Args:
            var (str): The name of the variable
            reg (str): The temporary register where the address is to be
                stored in case the given variable is a global

        Returns:
            str: The address where the variable is stored.

        """
        nonlocal mips
        if var in global_vars:
            mips += " " * indent + "la {}, {}\n".format(reg, var)
            return "({})".format(reg)
        else:
            for item in table.activation_record:
                if item[0] == var:
                    offset = item[1].activation_offset
            return "{}($fp)".format(offset)

    def store_pointer(var, reg):
        """Obtain the pointer to the address at which the variable is stored.

        Args:
            var (str): The name of the variable
            reg (str): The temporary register where the pointer is to be
                stored
        """
        nonlocal mips
        if var in global_vars:
            mips += " " * indent + "la {}, {}\n".format(reg, var)
        else:
            for item in table.activation_record:
                if item[0] == var:
                    offset = item[1].activation_offset
            mips += " " * indent + "addi {}, $fp, {}\n".format(reg, offset)

    orig_table = table  # Used when returning to global scope
    branch_count = 0  # For branch labels: "__branch_{}".format(branch_count)
    is_main = False  # For choosing whether to save data in static link or not
    func_call_dtypes = []  # Kept track for inbuilt functions

    for i, line in enumerate(ir_lines):
        # Make labels occupy a single separate line
        if ":" in line:
            mips += " " * indent + ":".join(line.split(":")[:-1]) + ":\n"
            line = line.split(":")[-1].strip()

        if line.startswith("func begin"):
            func_name = line.split()[-1]
            mips += " " * indent + "{}:\n".format(line.split()[-1])
            indent += 4

            if "." in func_name:  # Method or import
                rec_name = func_name.split(".")[0]
                meth_name = func_name.split(".")[1]
                try:
                    table = table.get_method((meth_name, rec_name), "body")
                except GoException:
                    # Package import
                    import_table = table.get_import(rec_name)[0]
                    table = import_table.get_func(meth_name, "body")[0]
            else:  # Function
                table = table.get_func(func_name, "body")[0]

            # Callee saving
            mips += " " * indent + "addi $sp, $sp, -4\n"
            mips += " " * indent + "sw $fp, ($sp)\n"  # dynamic link
            mips += " " * indent + "addi $fp, $sp, 4\n"  # stack pointer
            mips += " " * indent + "addi $sp, $sp, -4\n"
            mips += " " * indent + "sw $ra, ($sp)\n"  # return address
            mips += " " * indent + "addi $sp, $sp, -4\n"  # static link
            mips += " " * indent + "addi $sp, $sp, {}\n".format(
                table.activation_record[-1][1].activation_offset + 12
            )  # local variables (12 already subtracted)
            # TODO: String allocation

            # Move all global declarations into the main function.
            # Inserting them before the next index makes the loop process them
            # before processing the "main" function's code
            if func_name == "main":
                is_main = True
                for item in global_lines[::-1]:
                    ir_lines.insert(i + 1, item)

            elif func_name in inbuilt_funcs:
                if func_name == "println":
                    inbuilt_lines = [
                        "li $t0, 0",
                        "addi $t1, $fp, -16",
                        "__println_loop:",
                        "lw $v0, ($t1)",
                        "beqz $v0, __println_loop_end",
                        "li $t2, 1",
                        "bne $v0, $t2, __println_float",
                        "add $t2, $t0, $fp",
                        "lw $a0, ($t2)",
                        "j __println_end",
                        "__println_float:",
                        "li $t2, 2",
                        "bne $v0, $t2, __println_double",
                        "add $t2, $t0, $fp",
                        "l.s $f12, ($t2)",
                        "j __println_end",
                        "__println_double:",
                        "li $t2, 3",
                        "bne $v0, $t2, __println_string",
                        "add $t2, $t0, $fp",
                        "l.d $f12, ($t2)",
                        "addi $t0, $t0, 4",
                        "addi $t1, $t1, -4",
                        "j __println_end",
                        "__println_string:",
                        "add $t2, $t0, $fp",
                        "lw $a0, ($t2)",
                        "__println_end:",
                        "syscall",
                        "addi $t0, $t0, 4",
                        "addi $t1, $t1, -4",
                        "la $a0, __println_comma",
                        "li $v0, 4",
                        "syscall",
                        "j __println_loop",
                        "__println_loop_end:",
                        "la $a0, __println_newline",
                        "li $v0, 4",
                        "syscall",
                    ]
                    for code in inbuilt_lines:
                        mips += " " * indent + code + "\n"

        elif line == "func end":
            mips += "\n"
            indent -= 4
            table = orig_table
            is_main = False

        elif "=" in line and "= call " not in line:
            lhs = line.split(" = ")[0].strip()
            lhs_dtype = get_type(lhs)

            rhs = line.split(" = ")[1].strip().split()
            if len(rhs) == 1:
                indices = [0]
                # LHS dtype is used, as there are cases when RHS is a constant
                rhs_dtype = lhs_dtype
            elif len(rhs) == 3:
                indices = [0, 2]
                if re.fullmatch(r"[\d.]+", rhs[0]):
                    if re.fullmatch(r"[\d.]+", rhs[2]):
                        rhs_dtype = lhs_dtype
                    else:
                        rhs_dtype = get_type(rhs[2])
                else:
                    rhs_dtype = get_type(rhs[0])
            else:  # type-casting
                indices = [1]
                rhs_dtype = get_type(rhs[-1])

            rhs_size = table.get_size(rhs_dtype)
            if isinstance(rhs_dtype, GoArray) or isinstance(
                rhs_dtype, GoPointType
            ):
                is_float = False
                is_unsigned = True
            elif "float" in rhs_dtype.name:
                is_float = True
                is_unsigned = False
            else:
                is_float = False
                if "uint" in rhs_dtype.name:
                    is_unsigned = True
                else:
                    is_unsigned = False

            for reg, index in enumerate(indices):
                if is_float:
                    target = "$f{}".format(reg * 2)
                else:
                    target = "$t{}".format(reg)

                if re.fullmatch(r"[\d.]+", rhs[index]):  # RHS is a constant
                    if is_float:
                        mips += " " * indent + "{} $f{}, {}\n".format(
                            size2instr(rhs_size, "load", is_float=True),
                            reg * 2,
                            rhs[index],
                        )
                    else:
                        mips += " " * indent + "li $t{}, {}\n".format(
                            reg, rhs[index]
                        )

                elif rhs[index][0] == "*":  # Pointer dereference
                    source = get_addr(rhs[index][1:], "$t{}".format(reg))
                    mips += " " * indent + "lw $t{}, {}\n".format(reg, source)
                    instr = size2instr(rhs_size, "load", is_float, is_unsigned)
                    mips += " " * indent + "{} {}, ($t{})\n".format(
                        instr, target, reg
                    )

                elif rhs[index][0] == "&":  # Address of
                    store_pointer(rhs[index][1:], "$t{}".format(reg))

                elif "[" not in rhs[index]:
                    source = get_addr(rhs[index], "$t{}".format(reg))
                    mips += " " * indent + "{} {}, {}\n".format(
                        size2instr(rhs_size, "load", is_float, is_unsigned),
                        target,
                        source,
                    )

                else:
                    # Split the pointer and the index
                    items = re.search(
                        r"([\w_]+)\[([^\]]+)\]", rhs[index]
                    ).groups()
                    source = get_addr(items[0], "$t{}".format(reg))
                    if re.fullmatch(r"\d+", items[1]) is None:
                        # The index is a variable
                        ind_source = get_addr(items[1], "$t{}".format(reg + 1))
                        mips += " " * indent + "lw $t{}, {}\n".format(
                            reg, source
                        )
                        mips += " " * indent + "lw $t{}, {}\n".format(
                            reg + 1, ind_source
                        )
                        mips += (
                            " " * indent
                            + "add $t{0:}, $t{0:}, $t{1:}\n".format(
                                reg, reg + 1
                            )
                        )
                    else:
                        # The index is a number
                        mips += " " * indent + "lw $t{}, {}\n".format(
                            reg, source
                        )
                        mips += (
                            " " * indent
                            + "addi $t{0:}, $t{0:}, {1:}\n".format(
                                reg, items[1]
                            )
                        )
                    mips += " " * indent + "{} {}, ($t{})\n".format(
                        size2instr(rhs_size, "load", is_float, is_unsigned),
                        target,
                        reg,
                    )

            if len(rhs) == 3:
                if is_float:
                    if rhs_size > 4:
                        suffix = ".d"
                    else:
                        suffix = ".s"
                    op2instr = {
                        "+": "add." + suffix,
                        "-": "sub." + suffix,
                        "*": "mul." + suffix,
                        "/": "div." + suffix,
                    }
                    regs = ["$f0", "$f2"]
                else:
                    op2instr = {
                        "+": "addu",
                        "-": "subu",
                        "|": "or",
                        "^": "xor",
                        "<<": "sllv",
                        ">>": "srlv" if is_unsigned else "srav",
                        "&": "and",
                        "==": "seq",
                        "!=": "sne",
                        "<": "sltu" if is_unsigned else "slt",
                        "<=": "sleu" if is_unsigned else "sle",
                        ">": "sgtu" if is_unsigned else "sgt",
                        ">=": "sgeu" if is_unsigned else "sge",
                    }
                    regs = ["$t0", "$t1"]

                if rhs[1] in op2instr:
                    mips += " " * indent + "{0:} {1:}, {1:}, {2:}\n".format(
                        op2instr[rhs[1]], *regs
                    )

                elif rhs[1] == "&^":  # int only
                    mips += " " * indent + "and $t0, $t0, $t1\n"
                    mips += " " * indent + "nor $t0, $t0, $t0\n"

                elif rhs[1] == "||":  # bool only
                    # Set result to 1 in $t2 (to preserve $t0) by default.
                    # The branch will NOT set it to 0.
                    mips += " " * indent + "li $t2, 1\n"
                    mips += " " * indent + "bnez $t0, __branch_{}\n".format(
                        branch_count
                    )
                    mips += " " * indent + "bnez $t1, __branch_{}\n".format(
                        branch_count
                    )
                    mips += " " * indent + "li $t2, 0\n"
                    mips += (
                        " " * indent
                        + "__branch_{}: or $t0, $t2, $0\n".format(branch_count)
                    )

                elif rhs[1] == "&&":  # bool only
                    # Set result to 0 in $t2 (to preserve $t0) by default.
                    # The branch will NOT set it to 1.
                    mips += " " * indent + "li $t2, 0\n"
                    mips += " " * indent + "beqz $t0, __branch_{}\n".format(
                        branch_count
                    )
                    mips += " " * indent + "beqz $t1, __branch_{}\n".format(
                        branch_count
                    )
                    mips += " " * indent + "li $t2, 1\n"
                    # Copy $t2 to $t0
                    mips += (
                        " " * indent
                        + "__branch_{}: or $t0, $t2, $0\n".format(branch_count)
                    )

                elif rhs[1] in ["==", "!=", "<", "<=", ">", ">="]:
                    # float only; int already handled
                    if rhs_size > 4:
                        suffix = ".d"
                    else:
                        suffix = ".s"
                    if rhs[1] in ["==", "!="]:
                        mips += " " * indent + "c.eq{} $f0, $f2\n"
                    elif rhs[1] in ["<", ">="]:
                        mips += " " * indent + "c.lt{} $f0, $f2\n"
                    elif rhs[1] in ["<=", ">"]:
                        mips += " " * indent + "c.le{} $f0, $f2\n"

                    # Set result to 1 by default.
                    # The branch will NOT set it to 0.
                    mips += " " * indent + "li $t0, 1\n"
                    if rhs[1] in ["==", "<", "<="]:
                        instr = "bc1t"
                    else:
                        instr = "bc1f"
                    mips += " " * indent + "{} __branch_{}\n".format(
                        instr, branch_count
                    )
                    mips += " " * indent + "li $t0, 0\n"
                    # Branch to noop, to preserve indentation
                    mips += " " * indent + "__branch_{}:\n".format(
                        branch_count
                    )
                    branch_count += 1

                elif rhs[1] == "*":  # int only; float already handled
                    if is_unsigned:
                        instr = "multu"
                    else:
                        instr = "mult"
                    mips += " " * indent + "{} $t0, $t1\n".format(instr)
                    mips += " " * indent + "mflo $t0\n"

                elif rhs[1] in ["/", "%"]:  # int only; float already handled
                    mips += " " * indent + "div $t0, $t1\n"
                    if rhs[1] == "/":
                        mips += " " * indent + "mflo $t0\n"
                    else:
                        mips += " " * indent + "mfhi $t0\n"

            elif len(rhs) == 2:
                suffixes = []
                regs = []
                for item_dtype in [lhs_dtype, rhs_dtype]:
                    if (
                        hasattr(item_dtype, "name")
                        and "float" in item_dtype.name
                    ):
                        regs.append("$f0")
                        if table.get_size(item_dtype) > 4:
                            suffixes.append(".d")
                        else:
                            suffixes.append(".s")
                    else:
                        suffixes.append(".w")
                        regs.append("$t0")
                # Ignoring uint/int casts
                if suffixes[0] != suffixes[1]:
                    mips += " " * indent + "cvt{}{} {}, {}\n".format(
                        *suffixes, *regs
                    )

            if "[" not in lhs:  # No indexing in LHS
                dest = get_addr(lhs, "$t1")

            elif re.match(r".*\[.+\[", lhs) is None:  # Single indexing in LHS
                source = get_addr(lhs.split("[")[0], "$t1")
                mips += " " * indent + "lw $t1, {}\n".format(source)
                index_str = lhs.split("[")[1][:-1]

            else:  # Indexing in LHS
                remaining = lhs
                while re.match(r".*\[.+\[", remaining) is not None:
                    split = remaining.index("]") + 1
                    before = remaining[:split]
                    remaining = remaining[split:]

                    if before[0] != "[":  # First iteration
                        source = get_addr(before.split("[")[0], "$t1")
                        mips += " " * indent + "lw $t1, {}\n".format(source)
                        index_str = before.split("[")[1][:-1]
                    else:
                        index_str = before[1:-1]

                    if re.fullmatch(r"\d+", index_str):  # Numeric index
                        mips += " " * indent + "lw $t1, {}($t1)\n".format(
                            index_str
                        )
                    else:  # Index is a variable
                        ind_source = get_addr(index_str, "$t2")
                        mips += " " * indent + "lw $t2, {}\n".format(
                            ind_source
                        )
                        mips += " " * indent + "add $t1, $t1, $t2\n"
                        mips += " " * indent + "lw $t1, ($t1)\n"
                index_str = remaining[1:-1]

            if "[" in lhs:  # Indexing in LHS
                dest = "($t1)"
                if re.fullmatch(r"\d+", index_str):  # Numeric index
                    mips += " " * indent + "addi $t1, $t1, {}\n".format(
                        index_str
                    )
                else:  # Index is a variable
                    ind_source = get_addr(index_str, "$t2")
                    mips += " " * indent + "lw $t2, {}\n".format(ind_source)
                    mips += " " * indent + "add $t1, $t1, $t2\n".format(
                        index_str
                    )

            lhs_size = table.get_size(lhs_dtype)
            if isinstance(lhs_dtype, GoArray) or isinstance(
                lhs_dtype, GoPointType
            ):
                is_float = False
                is_unsigned = True
            elif "float" in lhs_dtype.name:
                is_float = True
                is_unsigned = False
            else:
                is_float = False
                if "uint" in lhs_dtype.name:
                    is_unsigned = True
                else:
                    is_unsigned = False

            mips += " " * indent + "{} {}, {}\n".format(
                size2instr(lhs_size, "store", is_float, is_unsigned),
                "$f0" if is_float else "$t0",
                dest,
            )

        elif line.startswith("if"):
            cond = line.split()[1]
            source = get_addr(cond, "$t0")
            target = line.split()[-1]
            # lb used as condition must be boolean
            mips += " " * indent + "lb $t0, {}\n".format(source)
            mips += " " * indent + "bnez $t0, {}\n".format(target)

        elif line.startswith("goto "):
            mips += " " * indent + line.replace("goto", "j") + "\n"

        elif line.startswith("param "):
            param = line.split()[1]
            # Store address of param in $t0
            store_pointer(param, "$t0")
            param_dtype = get_type(param)
            func_call_dtypes.append(param_dtype)
            # Aligning a/c to offsets
            param_size = ceil(table.get_size(param_dtype) / 4) * 4
            # Make space for param on the stack
            mips += " " * indent + "addi $sp, $sp, {}\n".format(
                -1 * param_size
            )
            # Store param on the stack
            for offset in range(0, param_size, 4):
                mips += " " * indent + "lw $t1, {}($t0)\n".format(offset)
                mips += " " * indent + "sw $t1, {}($sp)\n".format(offset)

        elif "= call " in line:
            func_name = line.split("= call ")[1].split()[0][:-1]
            lhs = line.split("= call ")[0].strip()

            # Return value is to be stored at address in static link
            store_pointer(lhs, "$t0")
            mips += " " * indent + "addi $sp, $sp, -12\n"
            mips += " " * indent + "sw $t0, ($sp)\n"

            if func_name in inbuilt_funcs:
                for param_dtype in func_call_dtypes:
                    if not isinstance(param_dtype, GoType):
                        raise NotImplementedError(
                            "Inbuilt functions only defined for inbuilt dtypes"
                        )
                    elif "float" in param_dtype.name:
                        if table.get_size(param_dtype) > 4:
                            syscall = 3  # double
                        else:
                            syscall = 2  # float
                    elif "string" in param_dtype.name:
                        syscall = 4  # string
                    else:
                        syscall = 1  # int
                    mips += " " * indent + "addi $sp, $sp, -4\n"
                    mips += " " * indent + "li $t0, {}\n".format(syscall)
                    mips += " " * indent + "sw $t0, ($sp)\n"
                mips += " " * indent + "addi $sp, $sp, -4\n"
                mips += " " * indent + "sw $0, ($sp)\n"
                mips += " " * indent + "addi $sp, $sp, {}\n".format(
                    4 * len(func_call_dtypes) + 4
                )

            mips += " " * indent + "addi $sp, $sp, 12\n"
            mips += " " * indent + "jal {}\n".format(func_name)
            # Erase, as function call is done
            func_call_dtypes = []

        elif line.startswith("return "):
            return_val = line.split()[-1]
            if return_val == "0" and not is_main:
                mips += " " * indent + "lw $t0, -12($fp)\n"
                mips += " " * indent + "sw $0, ($t0)\n"
                mips += " " * indent + "li $v0, 0\n"
            elif not is_main:
                return_dtype = get_type(return_val)
                return_size = ceil(table.get_size(return_dtype) / 4) * 4
                store_pointer(return_val, "$t0")
                mips += " " * indent + "lw $t1, -12($fp)\n"
                for offset in range(0, return_size, 4):
                    mips += " " * indent + "lw $t2, {}($t0)\n".format(offset)
                    mips += " " * indent + "sw $t2, {}($t1)\n".format(offset)
                if return_size <= 8:
                    if return_size > 4:
                        mips += " " * indent + "lw $v1, 4($t0)\n"
                    mips += " " * indent + "lw $v0, ($t0)\n"

            # Callee retrieving
            mips += " " * indent + "lw $ra, -8($fp)\n"  # return address
            mips += " " * indent + "move $sp, $fp\n"  # stack pointer
            mips += " " * indent + "lw $fp, -4($fp)\n"  # dynamic link
            mips += " " * indent + "jr $ra\n"

        else:
            logging.info("Unknown line: " + line)
    return mips.strip()


if __name__ == "__main__":
    argparser = ArgumentParser(description="Compiler for Go to MIPS")
    argparser.add_argument("input", type=str, help="input file")
    argparser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="output name for the MIPS assembly code",
    )
    argparser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug output"
    )
    args = argparser.parse_args()
    if args.output is None:
        # Output name is source filename (w/o extension) + ".s"
        args.output = (
            ".".join(args.input.split("/")[-1].split(".")[:-1]) + ".s"
        )

    if args.verbose:
        logging.getLogger().setLevel(logging.INFO)

    table, ir_code = process_code(args.input)
    mips = ir2mips(table, ir_code)
    with open(args.output, "w") as out_file:
        out_file.write(mips)
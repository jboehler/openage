# Copyright 2014-2020 the openage authors. See copying.md for legal info.

# TODO pylint: disable=C,R

import hashlib
import math
import struct

from openage.convert.value_object.init.game_version import GameEdition

from ....util.strings import decode_until_null
from ...deprecated.struct_definition import (StructDefinition, vararray_match,
                                             integer_match)
from ...deprecated.util import struct_type_lookup
from ...value_object.read.member_access import READ, READ_GEN, READ_UNKNOWN, NOREAD_EXPORT, SKIP
from ...value_object.read.read_members import (IncludeMembers, ContinueReadMember,
                                               MultisubtypeMember, GroupMember, SubdataMember,
                                               ReadMember,
                                               EnumLookupMember)
from ...value_object.read.value_members import ContainerMember, ArrayMember, IntMember, FloatMember,\
    StringMember, BooleanMember, IDMember, BitfieldMember
from ...value_object.read.value_members import MemberTypes as StorageType


class GenieStructure:
    """
    superclass for all structures from Genie Engine games.
    """

    # name of the created struct
    name_struct = None

    # name of the file where struct is placed in
    name_struct_file = None

    # comment for the created struct
    struct_description = None

    # struct format specification
    # ===========================================================
    # contains a list of 4-tuples that define
    # (read_mode, var_name, storage_type, read_type)
    #
    # read_mode: Tells whether to read or skip values
    # var_name: The stored name of the extracted variable.
    #           Must be unique for each ConverterObject
    # storage_type: ValueMember type for storage
    #               (see value_members.MemberTypes)
    # read_type: ReadMember type for reading the values from bytes
    #            (see read_members.py)
    # ===========================================================

    def __init__(self, **args):
        # store passed arguments as members
        self.__dict__.update(args)

    def read(self, raw, offset, game_version, cls=None, members=None):
        """
        recursively read defined binary data from raw at given offset.

        this is used to fill the python classes with data from the binary input.
        """
        if cls:
            target_class = cls
        else:
            target_class = self

        # Members are returned at the end
        generated_value_members = []

        # break out of the current reading loop when members don't exist in
        # source data file
        stop_reading_members = False

        if not members:
            members = target_class.get_data_format(game_version,
                                                   allowed_modes=(True,
                                                                  READ,
                                                                  READ_GEN,
                                                                  READ_UNKNOWN,
                                                                  SKIP),
                                                   flatten_includes=False)

        for _, export, var_name, storage_type, var_type in members:

            if stop_reading_members:
                if isinstance(var_type, ReadMember):
                    replacement_value = var_type.get_empty_value()
                else:
                    replacement_value = 0

                setattr(self, var_name, replacement_value)
                continue

            if isinstance(var_type, GroupMember):
                if not issubclass(var_type.cls, GenieStructure):
                    raise Exception("class where members should be "
                                    "included is not exportable: %s" % (
                                        var_type.cls.__name__))

                if isinstance(var_type, IncludeMembers):
                    # call the read function of the referenced class (cls),
                    # but store the data to the current object (self).
                    offset, gen_members = var_type.cls.read(self, raw, offset,
                                                            game_version,
                                                            cls=var_type.cls)

                    if export == READ_GEN:
                        # Push the passed members directly into the list of generated members
                        generated_value_members.extend(gen_members)

                else:
                    # create new instance of ValueMember,
                    # depending on the storage type.
                    # then save the result as a reference named `var_name`
                    grouped_data = var_type.cls()
                    offset, gen_members = grouped_data.read(raw, offset, game_version)

                    setattr(self, var_name, grouped_data)

                    if export == READ_GEN:
                        # Store the data
                        if storage_type is StorageType.CONTAINER_MEMBER:
                            # push the members into a ContainerMember
                            container = ContainerMember(var_name, gen_members)

                            generated_value_members.append(container)

                        elif storage_type is StorageType.ARRAY_CONTAINER:
                            # create a container for the members first, then push the
                            # container into an array
                            container = ContainerMember(var_name, gen_members)
                            allowed_member_type = StorageType.CONTAINER_MEMBER
                            array = ArrayMember(var_name, allowed_member_type, [container])

                            generated_value_members.append(array)

                        else:
                            raise Exception("%s at offset %# 08x: Data read via %s "
                                            "cannot be stored as %s;"
                                            " expected %s or %s"
                                            % (var_name, offset, var_type, storage_type,
                                               StorageType.CONTAINER_MEMBER,
                                               StorageType.ARRAY_CONTAINER))

            elif isinstance(var_type, MultisubtypeMember):
                # subdata reference implies recursive call for reading the
                # binary data

                # arguments passed to the next-level constructor.
                varargs = dict()

                if var_type.passed_args:
                    if isinstance(var_type.passed_args, str):
                        var_type.passed_args = set(var_type.passed_args)
                    for passed_member_name in var_type.passed_args:
                        varargs[passed_member_name] = getattr(
                            self, passed_member_name)

                # subdata list length has to be defined beforehand as a
                # object member OR number.  it's name or count is specified
                # at the subdata member definition by length.
                list_len = var_type.get_length(self)

                # prepare result storage lists
                if isinstance(var_type, SubdataMember):
                    # single-subtype child data list
                    setattr(self, var_name, list())
                    single_type_subdata = True
                else:
                    # multi-subtype child data list
                    setattr(self, var_name, {key: []
                                             for key in var_type.class_lookup})
                    single_type_subdata = False

                # List for storing the ValueMember instance of each subdata structure
                subdata_value_members = []
                allowed_member_type = StorageType.CONTAINER_MEMBER

                # check if entries need offset checking
                if var_type.offset_to:
                    offset_lookup = getattr(self, var_type.offset_to[0])
                else:
                    offset_lookup = None

                for i in range(list_len):

                    # List of subtype members filled if there's a subtype to be read
                    sub_members = []

                    # if datfile offset == 0, entry has to be skipped.
                    if offset_lookup:
                        if not var_type.offset_to[1](offset_lookup[i]):
                            continue
                        # TODO: don't read sequentially, use the lookup as
                        #       new offset?

                    if single_type_subdata:
                        # append single data entry to the subdata object list
                        new_data_class = var_type.class_lookup[None]
                    else:
                        # to determine the subtype class, read the binary
                        # definition. this utilizes an on-the-fly definition
                        # of the data to be read.
                        offset, sub_members = self.read(
                            raw, offset, game_version, cls=target_class,
                            members=(((False,) + var_type.subtype_definition),)
                        )

                        # read the variable set by the above read call to
                        # use the read data to determine the denominaton of
                        # the member type
                        subtype_name = getattr(
                            self, var_type.subtype_definition[1])

                        # look up the type name to get the subtype class
                        new_data_class = var_type.class_lookup[subtype_name]

                    if not issubclass(new_data_class, GenieStructure):
                        raise Exception("dumped data "
                                        "is not exportable: %s" % (
                                            new_data_class.__name__))

                    # create instance of submember class
                    new_data = new_data_class(**varargs)

                    # recursive call, read the subdata.
                    offset, gen_members = new_data.read(raw, offset, game_version, new_data_class)

                    # append the new data to the appropriate list
                    if single_type_subdata:
                        getattr(self, var_name).append(new_data)
                    else:
                        getattr(self, var_name)[subtype_name].append(new_data)

                    if export == READ_GEN:
                        # Append the data to the ValueMember list
                        if storage_type is StorageType.ARRAY_CONTAINER:
                            # Put the subtype members in front
                            sub_members.extend(gen_members)
                            gen_members = sub_members
                            # create a container for the retrieved members
                            container = ContainerMember(var_name, gen_members)

                            # Save the container to a list
                            # The array is created after the for-loop
                            subdata_value_members.append(container)

                        else:
                            raise Exception("%s at offset %# 08x: Data read via %s "
                                            "cannot be stored as %s;"
                                            " expected %s"
                                            % (var_name, offset, var_type, storage_type,
                                               StorageType.ARRAY_CONTAINER))

                if export == READ_GEN:
                    # Create an array from the subdata structures
                    # and append it to the other generated members
                    array = ArrayMember(var_name, allowed_member_type, subdata_value_members)
                    generated_value_members.append(array)

            else:
                # reading binary data, as this member is no reference but
                # actual content.

                data_count = 1
                is_array = False
                is_custom_member = False

                if isinstance(var_type, str):
                    is_array = vararray_match.match(var_type)

                    if is_array:
                        struct_type = is_array.group(1)
                        data_count = is_array.group(2)
                        if struct_type == "char":
                            struct_type = "char[]"

                        if integer_match.match(data_count):
                            # integer length
                            data_count = int(data_count)
                        else:
                            # dynamic length specified by member name
                            data_count = getattr(self, data_count)

                        if storage_type not in (StorageType.STRING_MEMBER,
                                                StorageType.ARRAY_INT,
                                                StorageType.ARRAY_FLOAT,
                                                StorageType.ARRAY_BOOL,
                                                StorageType.ARRAY_ID,
                                                StorageType.ARRAY_STRING):
                            raise Exception("%s at offset %# 08x: Data read via %s "
                                            "cannot be stored as %s;"
                                            " expected ArrayMember format"
                                            % (var_name, offset, var_type, storage_type))

                    else:
                        struct_type = var_type
                        data_count = 1

                elif isinstance(var_type, ReadMember):
                    # These could be EnumMember, EnumLookupMember, etc.

                    # special type requires having set the raw data type
                    struct_type = var_type.raw_type
                    data_count = var_type.get_length(self)
                    is_custom_member = True

                else:
                    raise Exception(
                        "unknown data member definition %s for member '%s'" % (var_type, var_name))

                if data_count < 0:
                    raise Exception("invalid length %d < 0 in %s for member '%s'" % (
                        data_count, var_type, var_name))

                if struct_type not in struct_type_lookup:
                    raise Exception("%s: member %s requests unknown data type %s" % (
                        repr(self), var_name, struct_type))

                if export == READ_UNKNOWN:
                    # for unknown variables, generate uid for the unknown
                    # memory location
                    var_name = "unknown-0x%08x" % offset

                # lookup c type to python struct scan type
                symbol = struct_type_lookup[struct_type]

                # read that stuff!!11
                struct_format = "< %d%s" % (data_count, symbol)

                if export != SKIP:
                    result = struct.unpack_from(struct_format, raw, offset)

                    if is_custom_member:
                        if not var_type.verify_read_data(self, result):
                            raise Exception("invalid data when reading %s "
                                            "at offset %# 08x" % (
                                                var_name, offset))

                    # TODO: move these into a read entry hook/verification method
                    if symbol == "s":
                        # stringify char array
                        result = decode_until_null(result[0])

                        if export == READ_GEN:
                            if storage_type is StorageType.STRING_MEMBER:
                                gen_member = StringMember(var_name, result)

                            else:
                                raise Exception("%s at offset %# 08x: Data read via %s "
                                                "cannot be stored as %s;"
                                                " expected %s"
                                                % (var_name, offset, var_type, storage_type,
                                                   StorageType.STRING_MEMBER))

                            generated_value_members.append(gen_member)

                    elif is_array:
                        if export == READ_GEN:
                            # Turn every element of result into a member
                            # and put them into an array
                            array_members = []
                            allowed_member_type = None

                            for elem in result:
                                if storage_type is StorageType.ARRAY_INT:
                                    gen_member = IntMember(var_name, elem)
                                    allowed_member_type = StorageType.INT_MEMBER
                                    array_members.append(gen_member)

                                elif storage_type is StorageType.ARRAY_FLOAT:
                                    gen_member = FloatMember(var_name, elem)
                                    allowed_member_type = StorageType.FLOAT_MEMBER
                                    array_members.append(gen_member)

                                elif storage_type is StorageType.ARRAY_BOOL:
                                    gen_member = BooleanMember(var_name, elem)
                                    allowed_member_type = StorageType.BOOLEAN_MEMBER
                                    array_members.append(gen_member)

                                elif storage_type is StorageType.ARRAY_ID:
                                    gen_member = IDMember(var_name, elem)
                                    allowed_member_type = StorageType.ID_MEMBER
                                    array_members.append(gen_member)

                                elif storage_type is StorageType.ARRAY_STRING:
                                    gen_member = StringMember(var_name, elem)
                                    allowed_member_type = StorageType.STRING_MEMBER
                                    array_members.append(gen_member)

                                else:
                                    raise Exception("%s at offset %# 08x: Data read via %s "
                                                    "cannot be stored as %s;"
                                                    " expected %s, %s, %s, %s or %s"
                                                    % (var_name, offset, var_type, storage_type,
                                                       StorageType.ARRAY_INT,
                                                       StorageType.ARRAY_FLOAT,
                                                       StorageType.ARRAY_BOOL,
                                                       StorageType.ARRAY_ID,
                                                       StorageType.ARRAY_STRING))

                            # Create the array
                            array = ArrayMember(var_name, allowed_member_type, array_members)
                            generated_value_members.append(array)

                    elif data_count == 1:
                        # store first tuple element
                        result = result[0]

                        if symbol == "f":
                            if not math.isfinite(result):
                                raise Exception("invalid float when "
                                                "reading %s at offset %# 08x" % (
                                                    var_name, offset))

                        if export == READ_GEN:
                            # Store the member as ValueMember
                            if is_custom_member:
                                lookup_result = var_type.entry_hook(result)

                                if isinstance(var_type, EnumLookupMember):
                                    # store differently depending on storage type
                                    if storage_type is StorageType.INT_MEMBER:
                                        # store as plain integer value
                                        gen_member = IntMember(var_name, result)

                                    elif storage_type is StorageType.ID_MEMBER:
                                        # store as plain integer value
                                        gen_member = IDMember(var_name, result)

                                    elif storage_type is StorageType.BITFIELD_MEMBER:
                                        # store as plain integer value
                                        gen_member = BitfieldMember(var_name, result)

                                    elif storage_type is StorageType.STRING_MEMBER:
                                        # store by looking up value from dict
                                        gen_member = StringMember(var_name, lookup_result)

                                    else:
                                        raise Exception("%s at offset %# 08x: Data read via %s "
                                                        "cannot be stored as %s;"
                                                        " expected %s, %s, %s or %s"
                                                        % (var_name, offset, var_type, storage_type,
                                                           StorageType.INT_MEMBER,
                                                           StorageType.ID_MEMBER,
                                                           StorageType.BITFIELD_MEMBER,
                                                           StorageType.STRING_MEMBER))

                                elif isinstance(var_type, ContinueReadMember):
                                    if storage_type is StorageType.BOOLEAN_MEMBER:
                                        gen_member = StringMember(var_name, lookup_result)

                                    else:
                                        raise Exception("%s at offset %# 08x: Data read via %s "
                                                        "cannot be stored as %s;"
                                                        " expected %s"
                                                        % (var_name, offset, var_type, storage_type,
                                                           StorageType.BOOLEAN_MEMBER))

                            else:
                                if storage_type is StorageType.INT_MEMBER:
                                    gen_member = IntMember(var_name, result)

                                elif storage_type is StorageType.FLOAT_MEMBER:
                                    gen_member = FloatMember(var_name, result)

                                elif storage_type is StorageType.BOOLEAN_MEMBER:
                                    gen_member = BooleanMember(var_name, result)

                                elif storage_type is StorageType.ID_MEMBER:
                                    gen_member = IDMember(var_name, result)

                                else:
                                    raise Exception("%s at offset %# 08x: Data read via %s "
                                                    "cannot be stored as %s;"
                                                    " expected %s, %s, %s or %s"
                                                    % (var_name, offset, var_type, storage_type,
                                                       StorageType.INT_MEMBER,
                                                       StorageType.FLOAT_MEMBER,
                                                       StorageType.BOOLEAN_MEMBER,
                                                       StorageType.ID_MEMBER))

                            generated_value_members.append(gen_member)

                    # run entry hook for non-primitive members
                    if is_custom_member:
                        result = var_type.entry_hook(result)

                        if result == ContinueReadMember.Result.ABORT:
                            # don't go through all other members of this class!
                            stop_reading_members = True

                    # store member's data value
                    setattr(self, var_name, result)

                # increase the current file position by the size we just read
                offset += struct.calcsize(struct_format)

        return offset, generated_value_members

    @classmethod
    def structs(cls):
        """
        create struct definitions for this class and its subdata references.

        TODO: Remove from buildsystem
        """

        ret = list()
        self_member_count = 0

        # dummy game edition that represents AoC
        dummy = GameEdition("", "AoC", "yes", [], [], [], [])

        # acquire all struct members, including the included members
        members = cls.get_data_format((dummy, []),
                                      allowed_modes=(True, SKIP, READ_GEN, NOREAD_EXPORT),
                                      flatten_includes=False)

        for _, _, _, _, member_type in members:
            self_member_count += 1
            if isinstance(member_type, MultisubtypeMember):
                for _, subtype_class in sorted(member_type.class_lookup.items()):
                    if not issubclass(subtype_class, GenieStructure):
                        raise Exception("tried to export structs "
                                        "from non-exportable %s" % (
                                            subtype_class))
                    ret += subtype_class.structs()

            elif isinstance(member_type, GroupMember):
                if not issubclass(member_type.cls, GenieStructure):
                    raise Exception("tried to export structs "
                                    "from non-exportable member "
                                    "included class %r" % (member_type.cls))
                ret += member_type.cls.structs()

            else:
                continue

        # create struct only when it has members?
        if True or self_member_count > 0:
            new_def = StructDefinition(cls)
            ret.append(new_def)

        return ret

    @classmethod
    def format_hash(cls, game_version, hasher=None):
        """
        provides a deterministic hash of all exported structure members

        used for determining changes in the exported data, which requires
        data reconversion.
        """

        if not hasher:
            hasher = hashlib.sha512()

        # struct properties
        hasher.update(cls.name_struct.encode())
        hasher.update(cls.name_struct_file.encode())
        hasher.update(cls.struct_description.encode())

        # only hash exported struct members!
        # non-exported values don't influence anything.
        members = cls.get_data_format(game_version,
                                      allowed_modes=(True, SKIP, READ_GEN, NOREAD_EXPORT),
                                      flatten_includes=False,
                                      )
        for _, export, member_name, _, member_type in members:
            # includemembers etc have no name.
            if member_name:
                hasher.update(member_name.encode())

            if isinstance(member_type, ReadMember):
                hasher = member_type.format_hash(hasher)

            elif isinstance(member_type, str):
                hasher.update(member_type.encode())

            else:
                raise Exception("can't hash unsupported member")

            hasher.update(export.name.encode())

        return hasher

    @classmethod
    def get_effective_type(cls):
        return cls.name_struct

    @classmethod
    def get_data_format(cls, game_version, allowed_modes=False,
                        flatten_includes=False, is_parent=False):
        """
        return all members of this exportable (a struct.)

        can filter by export modes and can also return included members:
        inherited members can either be returned as to-be-included,
        or can be fetched and displayed as if they weren't inherited.
        """
        for member in cls.get_data_format_members(game_version):
            if len(member) != 4:
                print(member[1])

            export, _, _, read_type = member

            definitively_return_member = False

            if isinstance(read_type, IncludeMembers):
                if flatten_includes:
                    # recursive call
                    yield from read_type.cls.get_data_format(game_version,
                                                             allowed_modes,
                                                             flatten_includes,
                                                             is_parent=True)
                    continue

            elif isinstance(read_type, ContinueReadMember):
                definitively_return_member = True

            if allowed_modes:
                if export not in allowed_modes:
                    if not definitively_return_member:
                        continue

            member_entry = (is_parent,) + member
            yield member_entry

    @classmethod
    def get_data_format_members(cls, game_version):
        """
        Return the members in this struct.
        """
        raise NotImplementedError("Subclass has not implemented this function")

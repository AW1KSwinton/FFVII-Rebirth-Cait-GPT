#pragma once
#include "object_path_cache.hpp"
#include <array>
#include <bit>
#include <Unreal/FWeakObjectPtr.hpp>
#include <Unreal/UObject.hpp>
#include <Unreal/UObjectArray.hpp>

namespace RC::LuaType {
    RC_UE4SS_API auto auto_construct_object(const LuaMadeSimple::Lua&, Unreal::UObject*) -> void;
}

namespace FF7RTelemetryExporter {
struct ObjectPathBindingContext : RC::Unreal::FUObjectDeleteListener {
    ObjectPathCache cache;
    std::shared_ptr<std::atomic<DWORD>> game_thread;
    std::atomic<uint64_t> weak_hits{};
    std::atomic<uint64_t> searches_allowed{};
    std::mutex zero_mutex;
    UnversionedObjectRegistry zero_objects;
    bool listening{};
    void NotifyUObjectDeleted(const RC::Unreal::UObjectBase*, int32_t index) override {
        std::lock_guard guard(zero_mutex);
        zero_objects.retire(index);
    }
    void OnUObjectArrayShutdown() override {
        { std::lock_guard guard(zero_mutex); zero_objects.clear(); listening = false; }
        RC::Unreal::UObjectArray::RemoveUObjectDeleteListener(this);
    }
    ~ObjectPathBindingContext() {
        if (listening) RC::Unreal::UObjectArray::RemoveUObjectDeleteListener(this);
    }
};
inline std::atomic<std::shared_ptr<ObjectPathBindingContext>> object_path_context;

inline ObjectIdentity fresh_object_identity(RC::Unreal::UObject* object) {
    RC::Unreal::FWeakObjectPtr weak;
    weak = object; // Only a freshly obtained object is accessed here.
    static_assert(sizeof(weak) == sizeof(std::array<int32_t, 2>));
    const auto parts = std::bit_cast<std::array<int32_t, 2>>(weak);
    return {parts[0], parts[1], reinterpret_cast<uintptr_t>(object)};
}

inline std::optional<ObjectIdentity> live_object_identity(ObjectIdentity identity) {
    // The pinned UE4SS FWeakObjectPtr consists of index and serial only.
    // Get checks the live registry slot/generation and pending-kill state
    // BEFORE returning a UObject; a stored address is never dereferenced.
    const auto weak = std::bit_cast<RC::Unreal::FWeakObjectPtr>(
        std::array<int32_t, 2>{identity.index, identity.serial});
    auto* current = weak.Get();
    if (!current) return {};
    return ObjectIdentity{identity.index, identity.serial, reinterpret_cast<uintptr_t>(current)};
}

inline void register_object_path_bindings(RC::LuaMadeSimple::Lua& lua,
        std::shared_ptr<std::atomic<DWORD>> game_thread) {
    if (!object_path_context.load()) {
        auto context = std::make_shared<ObjectPathBindingContext>();
        context->game_thread = std::move(game_thread);
        RC::Unreal::UObjectArray::AddUObjectDeleteListener(context.get());
        context->listening = true;
        object_path_context.store(std::move(context));
    }
    lua.register_function("FF7RRememberObjectPath", [](const RC::LuaMadeSimple::Lua& call) -> int {
        const auto context = object_path_context.load();
        if (!context || context->game_thread->load() != GetCurrentThreadId() || !call.is_string(1) || !call.is_integer(2)) return 0;
        const std::string path{call.get_string(1)};
        if (path.empty() || path.size() > 2048) return 0;
        // GetAddress is read from a fresh Lua wrapper within this callback.
        // get_string removes argument 1 in the pinned LuaMadeSimple API.
        auto* object = reinterpret_cast<RC::Unreal::UObject*>(call.get_integer(1));
        if (object) {
            const auto identity = fresh_object_identity(object);
            if (identity.serial == 0) {
                std::lock_guard guard(context->zero_mutex);
                if (context->listening) context->zero_objects.remember(identity);
            }
            context->cache.remember(path, identity);
        }
        return 0;
    });
    lua.register_function("FF7RResolveObjectPath", [](const RC::LuaMadeSimple::Lua& call) -> int {
        ObjectPathCache::Result result;
        const auto context = object_path_context.load();
        if (context && context->game_thread->load() == GetCurrentThreadId() && call.is_string(1)) {
            const std::string path{call.get_string(1)};
            if (!path.empty() && path.size() <= 2048) {
                // Hold the deletion guard through construction of the fresh
                // wrapper. Generation-zero objects have no weak serial.
                std::lock_guard guard(context->zero_mutex);
                auto snapshot = [&context](ObjectIdentity identity) {
                    return identity.serial == 0 ? context->zero_objects.current(identity) : live_object_identity(identity);
                };
                result = context->cache.resolve(path, std::chrono::steady_clock::now(), snapshot);
                if (result.address) {
                    auto* current = reinterpret_cast<RC::Unreal::UObject*>(result.address);
                    const auto full = RC::to_string(current->GetFullName());
                    const auto separator = full.find(' ');
                    if (separator == std::string::npos || full.substr(separator + 1) != path) {
                        context->cache.invalidate(path);
                        result = context->cache.resolve(path, std::chrono::steady_clock::now(), snapshot);
                    }
                }
                if (result.address) ++context->weak_hits;
                if (result.allow_search) ++context->searches_allowed;
                RC::LuaType::auto_construct_object(call, reinterpret_cast<RC::Unreal::UObject*>(result.address));
                call.set_bool(result.allow_search);
                return 2;
            }
        }
        RC::LuaType::auto_construct_object(call, reinterpret_cast<RC::Unreal::UObject*>(result.address));
        call.set_bool(result.allow_search);
        return 2;
    });
}
}

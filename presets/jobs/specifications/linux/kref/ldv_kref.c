#ifndef _KREF_H_
#define _KREF_H_

#include <linux/kobject.h>
#include <linux/types.h>
#include <linux/kobject.h>
#include <linux/kref.h>
#include <linux/spinlock.h>
#include <linux/refcount.h>

struct kref;

// kref_init refcount_set не содержит EXPORT_SYMBOL, Но будет добавлен
static inline void ldv_refcount_set(refcount_t *r, int n)
{
	atomic_set(&r->refs, n);
}

static inline void ldv_kref_init(struct kref *kref)
{
	ldv_refcount_set(&kref->refcount, 1);
}

// kref_read не содержит EXPORT_SYMBOL, Но будет добавлен
static inline unsigned int ldv_kref_read(const struct kref *kref)
{
	return ldv_refcount_read(&kref->refcount);
}
static inline unsigned int ldv_refcount_read(const refcount_t *r)
{
	return atomic_read(&r->refs);
}

void ldv_put_device(struct device *dev)
{
	/* might_sleep(); */
	if (dev)
		ldv_kobject_put(&dev->kobj);
}

void ldv_kobject_put(struct kobject *kobj)
{
	if (kobj) {
		if (!kobj->state_initialized)
			WARN(1, KERN_WARNING
				"kobject: '%s' (%p): is not initialized, yet kobject_put() is being called.\n",
			     kobject_name(kobj), kobj);
		ldv_kref_put(&kobj->kref, kobject_release);
	}
}

// kobj_to_dev не содержит EXPORT_SYMBOL
struct device *ldv_get_device(struct device *dev)
{
	return dev ? kobj_to_dev(ldv_kobject_get(&dev->kobj)) : NULL;
}

struct kobject *ldv_kobject_get(struct kobject *kobj)
{
	if (kobj) {
		if (!kobj->state_initialized)
			WARN(1, KERN_WARNING
				"kobject: '%s' (%p): is not initialized, yet kobject_get() is being called.\n",
			     kobject_name(kobj), kobj);
		ldv_kref_get(&kobj->kref);
	}
	return kobj;
}

//kobject_release, kobject_name не содержит EXPORT_SYMBOL
/*
static inline const char *ldv_kobject_name(const struct kobject *kobj)
{
	return kobj->name;
}
*/

//Не знаю, что я должен делать с этим, но оставлю коммент
/*
 * @release: pointer to the function that will clean up the object when the
 *	     last reference to the object is released.
 *	     This pointer is required, and it is not acceptable to pass kfree
 *	     in as this function.
*/

static inline int ldv_kref_put(struct kref *kref, void (*release)(struct kref *kref))
{
	if (refcount_dec_and_test(&kref->refcount)) {
		release(kref);
		return 1;
	}
	return 0;
}

// refcount_dec_and_test, refcount_sub_and_test, atomic_fetch_sub_release не содержит EXPORT_SYMBOL
/*
static inline __must_check bool ldv_refcount_dec_and_test(refcount_t *r)
{
	return ldv_refcount_sub_and_test(1, r);
}

static inline __must_check bool ldv_refcount_sub_and_test(int i, refcount_t *r)
{
	int old = ldv_atomic_fetch_sub_release(i, &r->refs);

	if (old == i) {
		smp_acquire__after_ctrl_dep();
		return true;
	}

	if (unlikely(old < 0 || old - i < 0))
		refcount_warn_saturate(r, REFCOUNT_SUB_UAF);

	return false;
}

static inline int ldv_atomic_fetch_sub_release(int i, atomic_t *v)
{
	__atomic_release_fence();
	return atomic_fetch_sub_relaxed(i, v);
}
*/

// kref_get не содержит EXPORT_SYMBOL, Но будет добавлен
static inline void ldv_kref_get(struct kref *kref)
{
	ldv_refcount_inc(&kref->refcount);
}
static inline void ldv_refcount_inc(refcount_t *r)
{
	ldv_refcount_add(1, r);
}

// refcount_add не содержит EXPORT_SYMBOL, Но будет добавлен
static inline void ldv_refcount_add(int i, refcount_t *r)
{
	int old = ldv_atomic_fetch_add_relaxed(i, &r->refs);

	if (unlikely(!old))
		refcount_warn_saturate(r, REFCOUNT_ADD_UAF);
	else if (unlikely(old < 0 || old + i < 0))
		refcount_warn_saturate(r, REFCOUNT_ADD_OVF);
}

//эта функция требует двойной проверки, см так же .aspect
/*
static inline int ldv_atomic_fetch_add_relaxed(int i, atomic_t *v)
{
	ldv_kasan_check_write(v, sizeof(*v));
	return ldv_arch_atomic_fetch_add_relaxed(i, v);
}

static inline bool ldv_kasan_check_write(const volatile void *p, unsigned int size)
{
	return true;
}
*/



